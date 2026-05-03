"""Juicer configuration — Pydantic models, registry store, and JSON store.

Registry paths match the C++ implementation exactly:
  ``HKLM\\System\\CurrentControlSet\\Services\\juicer\\boot``
  ``HKLM\\System\\CurrentControlSet\\Services\\juicer\\shutdown``
"""

from __future__ import annotations

import abc
import json
import logging
import platform
from enum import IntEnum
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────
# Constants matching C++ registry layout
# ──────────────────────────────────────────────────────────────────────

REG_ROOT = r"System\CurrentControlSet\Services\juicer"
REG_BOOT_SUBKEY = "boot"
REG_SHUTDOWN_SUBKEY = "shutdown"
REG_BOOTKEY = f"{REG_ROOT}\\{REG_BOOT_SUBKEY}"
REG_SHUTDOWNKEY = f"{REG_ROOT}\\{REG_SHUTDOWN_SUBKEY}"


# ──────────────────────────────────────────────────────────────────────
# Enums
# ──────────────────────────────────────────────────────────────────────


class BankAction(IntEnum):
    """Action to perform on a bank during a power sequence.

    Matches C++ registry: 0 = OFF, 1 = ON.
    ``None`` / absent registry value means "skip this bank".
    """

    OFF = 0
    ON = 1


# ──────────────────────────────────────────────────────────────────────
# Pydantic Models
# ──────────────────────────────────────────────────────────────────────


class BankConfig(BaseModel):
    """Configuration for one bank in a boot/shutdown sequence.

    If ``action`` is ``None`` the bank is skipped entirely (matches C++ behaviour
    when the registry value is absent).
    """

    action: BankAction | None = None
    pre_delay_ms: int = Field(default=0, ge=0, description="Milliseconds before action")
    post_delay_ms: int = Field(default=0, ge=0, description="Milliseconds after action")

    @field_validator("pre_delay_ms", "post_delay_ms")
    @classmethod
    def _reject_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("Delay cannot be negative")
        return v


class SequenceConfig(BaseModel):
    """Per-sequence (boot or shutdown) configuration for all four banks."""

    bank1: BankConfig = Field(default_factory=BankConfig)
    bank2: BankConfig = Field(default_factory=BankConfig)
    bank3: BankConfig = Field(default_factory=BankConfig)
    bank4: BankConfig = Field(default_factory=BankConfig)

    def bank(self, n: int) -> BankConfig:
        """Get bank config by number (1–4)."""
        return cast(BankConfig, getattr(self, f"bank{n}"))

    def set_bank(self, n: int, cfg: BankConfig) -> None:
        """Set bank config by number (1–4)."""
        setattr(self, f"bank{n}", cfg)


class GlobalConfig(BaseModel):
    """Top-level Juicer configuration combining both sequences and global settings."""

    port: str = Field(default="COM3", min_length=1, description="Serial port name")
    verbose: bool = False
    start_sound: str = ""
    stop_sound: str = ""
    boot: SequenceConfig = Field(default_factory=SequenceConfig)
    shutdown: SequenceConfig = Field(default_factory=SequenceConfig)

    @field_validator("port")
    @classmethod
    def _validate_port(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Port must not be empty")
        return v.strip()

    @model_validator(mode="after")
    def _validate_config(self) -> "GlobalConfig":
        return self


# ──────────────────────────────────────────────────────────────────────
# Store Abstract Base Class
# ──────────────────────────────────────────────────────────────────────


class ConfigStore(abc.ABC):
    """Abstract interface for reading/writing Juicer configuration."""

    @abc.abstractmethod
    def load(self) -> GlobalConfig:
        """Load and return the full configuration."""

    @abc.abstractmethod
    def save(self, config: GlobalConfig) -> None:
        """Persist the configuration."""


# ──────────────────────────────────────────────────────────────────────
# JSON Store (cross-platform, import/export)
# ──────────────────────────────────────────────────────────────────────


class JsonStore(ConfigStore):
    """Read/write configuration as a JSON file.

    Useful for import/export and for testing on non-Windows systems.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> GlobalConfig:
        """Load config from JSON file. Raises ``FileNotFoundError`` if missing."""
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return GlobalConfig.model_validate(data)

    def save(self, config: GlobalConfig) -> None:
        """Save config to JSON file with pretty formatting."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            config.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )
        logger.info("Config saved to %s", self.path)


# ──────────────────────────────────────────────────────────────────────
# Windows Registry Store
# ──────────────────────────────────────────────────────────────────────


def _is_windows() -> bool:
    return platform.system() == "Windows"


class WindowsRegistryStore(ConfigStore):
    """Read/write configuration from Windows registry.

    Registry layout matches the C++ implementation exactly::

        HKLM\\System\\CurrentControlSet\\Services\\juicer\\boot\\
            Verbose      REG_DWORD
            StartSound   REG_SZ
            StopSound    REG_SZ
            Port         REG_SZ
            Bank1Action  REG_DWORD
            Bank1PreDelay  REG_DWORD
            Bank1PostDelay REG_DWORD
            ...

    The same layout is repeated under ``\\shutdown``.

    This class is importable on any OS but raises ``OSError`` at runtime
    when methods are called on non-Windows platforms.
    """

    def __init__(self) -> None:
        if not _is_windows():
            logger.warning(
                "WindowsRegistryStore instantiated on %s — "
                "load()/save() will raise OSError at runtime.",
                platform.system(),
            )

    def _ensure_windows(self) -> None:
        if not _is_windows():
            raise OSError("WindowsRegistryStore requires Windows")

    def _read_sequence(self, subkey: str) -> tuple[SequenceConfig, dict[str, Any]]:
        """Read a single sequence (boot or shutdown) from registry.

        Returns ``(SequenceConfig, common_values_dict)``.
        """
        import winreg

        key_path = f"{REG_ROOT}\\{subkey}"
        common: dict[str, Any] = {}
        seq = SequenceConfig()

        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path, 0, winreg.KEY_READ)
        except FileNotFoundError:
            logger.warning("Registry key not found: HKLM\\%s", key_path)
            return seq, common

        try:
            # Read common values
            common["verbose"] = self._read_dword(key, "Verbose", 0) != 0
            common["start_sound"] = self._read_sz(key, "StartSound", "")
            common["stop_sound"] = self._read_sz(key, "StopSound", "")
            common["port"] = self._read_sz(key, "Port", "COM3")

            # Read per-bank configs
            for n in range(1, 5):
                action_raw = self._read_dword_optional(key, f"Bank{n}Action")
                if action_raw is None:
                    cfg = BankConfig(action=None)
                else:
                    cfg = BankConfig(
                        action=BankAction(action_raw) if action_raw in (0, 1) else None,
                        pre_delay_ms=self._read_dword(key, f"Bank{n}PreDelay", 0),
                        post_delay_ms=self._read_dword(key, f"Bank{n}PostDelay", 0),
                    )
                seq.set_bank(n, cfg)
        finally:
            winreg.CloseKey(key)

        return seq, common

    def _write_sequence(
        self, subkey: str, seq: SequenceConfig, common: dict[str, Any]
    ) -> None:
        """Write a single sequence to registry, creating keys as needed."""
        import winreg

        key_path = f"{REG_ROOT}\\{subkey}"
        key = winreg.CreateKeyEx(
            winreg.HKEY_LOCAL_MACHINE, key_path, 0, winreg.KEY_WRITE
        )
        try:
            # Common values
            winreg.SetValueEx(key, "Verbose", 0, winreg.REG_DWORD, int(common.get("verbose", 0)))
            winreg.SetValueEx(key, "StartSound", 0, winreg.REG_SZ, common.get("start_sound", ""))
            winreg.SetValueEx(key, "StopSound", 0, winreg.REG_SZ, common.get("stop_sound", ""))
            winreg.SetValueEx(key, "Port", 0, winreg.REG_SZ, common.get("port", "COM3"))

            # Per-bank
            for n in range(1, 5):
                bank_cfg = seq.bank(n)
                if bank_cfg.action is not None:
                    winreg.SetValueEx(
                        key, f"Bank{n}Action", 0, winreg.REG_DWORD, bank_cfg.action.value
                    )
                    winreg.SetValueEx(
                        key, f"Bank{n}PreDelay", 0, winreg.REG_DWORD, bank_cfg.pre_delay_ms
                    )
                    winreg.SetValueEx(
                        key, f"Bank{n}PostDelay", 0, winreg.REG_DWORD, bank_cfg.post_delay_ms
                    )
                else:
                    # Remove values for skipped banks
                    for val_name in (f"Bank{n}Action", f"Bank{n}PreDelay", f"Bank{n}PostDelay"):
                        try:
                            winreg.DeleteValue(key, val_name)
                        except FileNotFoundError:
                            pass
        finally:
            winreg.CloseKey(key)

    def load(self) -> GlobalConfig:
        """Load configuration from both boot and shutdown registry keys."""
        self._ensure_windows()

        boot_seq, boot_common = self._read_sequence(REG_BOOT_SUBKEY)
        shutdown_seq, shutdown_common = self._read_sequence(REG_SHUTDOWN_SUBKEY)

        # Use boot common values as authoritative (they should be the same)
        common = boot_common if boot_common.get("port") else shutdown_common

        return GlobalConfig(
            port=common.get("port", "COM3"),
            verbose=common.get("verbose", False),
            start_sound=common.get("start_sound", ""),
            stop_sound=common.get("stop_sound", ""),
            boot=boot_seq,
            shutdown=shutdown_seq,
        )

    def save(self, config: GlobalConfig) -> None:
        """Save configuration to both boot and shutdown registry keys."""
        self._ensure_windows()

        common = {
            "port": config.port,
            "verbose": config.verbose,
            "start_sound": config.start_sound,
            "stop_sound": config.stop_sound,
        }
        self._write_sequence(REG_BOOT_SUBKEY, config.boot, common)
        self._write_sequence(REG_SHUTDOWN_SUBKEY, config.shutdown, common)
        logger.info("Config saved to Windows registry")

    # ── Registry value helpers ────────────────────────────────────────

    @staticmethod
    def _read_dword(key: Any, name: str, default: int) -> int:
        import winreg

        try:
            value, reg_type = winreg.QueryValueEx(key, name)
            if reg_type == winreg.REG_DWORD:
                return int(value)
        except FileNotFoundError:
            pass
        return default

    @staticmethod
    def _read_dword_optional(key: Any, name: str) -> int | None:
        import winreg

        try:
            value, reg_type = winreg.QueryValueEx(key, name)
            if reg_type == winreg.REG_DWORD:
                return int(value)
        except FileNotFoundError:
            pass
        return None

    @staticmethod
    def _read_sz(key: Any, name: str, default: str) -> str:
        import winreg

        try:
            value, reg_type = winreg.QueryValueEx(key, name)
            if reg_type == winreg.REG_SZ and value:
                return str(value)
        except FileNotFoundError:
            pass
        return default
