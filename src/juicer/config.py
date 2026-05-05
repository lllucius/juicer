"""Juicer configuration — Pydantic models and TOML-backed storage."""

from __future__ import annotations

import abc
import logging
import os
import platform
import tempfile
import tomllib
from enum import IntEnum
from pathlib import Path
from typing import cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

logger = logging.getLogger(__name__)


class BankAction(IntEnum):
    """Action to perform on a bank during a power sequence.

    Matches the device protocol/config convention: 0 = OFF, 1 = ON.
    ``None`` means "skip this bank".
    """

    OFF = 0
    ON = 1


class BankConfig(BaseModel):
    """Configuration for one bank in a boot/shutdown sequence."""

    model_config = ConfigDict(validate_assignment=True)

    action: BankAction | None = None
    pre_delay_ms: int = Field(default=0, ge=0, description="Milliseconds before action")
    post_delay_ms: int = Field(default=0, ge=0, description="Milliseconds after action")


class SequenceConfig(BaseModel):
    """Per-sequence configuration for all four banks."""

    model_config = ConfigDict(validate_assignment=True)

    bank1: BankConfig = Field(default_factory=BankConfig)
    bank2: BankConfig = Field(default_factory=BankConfig)
    bank3: BankConfig = Field(default_factory=BankConfig)
    bank4: BankConfig = Field(default_factory=BankConfig)

    def bank(self, n: int) -> BankConfig:
        """Get bank config by number (1–4)."""
        if n not in range(1, 5):
            raise ValueError(f"Bank number must be 1-4, got {n}")
        return cast(BankConfig, getattr(self, f"bank{n}"))

    def set_bank(self, n: int, cfg: BankConfig) -> None:
        """Set bank config by number (1–4)."""
        if n not in range(1, 5):
            raise ValueError(f"Bank number must be 1-4, got {n}")
        setattr(self, f"bank{n}", cfg)


class GlobalConfig(BaseModel):
    """Top-level Juicer configuration."""

    model_config = ConfigDict(validate_assignment=True)

    port: str = Field(default="COM3", min_length=1, description="Serial port name")
    event_start_sound: str = ""
    event_stop_sound: str = ""
    boot: SequenceConfig = Field(default_factory=SequenceConfig)
    shutdown: SequenceConfig = Field(default_factory=SequenceConfig)

    @field_validator("port")
    @classmethod
    def _strip_port(cls, v: str) -> str:
        return v.strip()


class ConfigStore(abc.ABC):
    """Abstract interface for reading/writing Juicer configuration."""

    @abc.abstractmethod
    def load(self) -> GlobalConfig:
        """Load and return the full configuration."""

    @abc.abstractmethod
    def save(self, config: GlobalConfig) -> None:
        """Persist the configuration."""


class TomlStore(ConfigStore):
    """Read/write configuration as a TOML file."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_config_path()

    def load(self) -> GlobalConfig:
        """Load config from TOML file. Raises ``FileNotFoundError`` if missing."""
        with self.path.open("rb") as file:
            data = tomllib.load(file)
        return GlobalConfig.model_validate(data)

    def save(self, config: GlobalConfig) -> None:
        """Atomically save config to a TOML file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        content = dump_config_toml(config)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
            text=True,
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            os.replace(tmp_path, self.path)
        except Exception:
            try:
                tmp_path.unlink(missing_ok=True)
            finally:
                raise
        logger.info("Config saved to %s", self.path)


def default_config_path() -> Path:
    """Return the default TOML config path for this platform."""
    if platform.system() == "Windows":
        base = os.environ.get("PROGRAMDATA")
        if base:
            return Path(base) / "Juicer" / "config.toml"
    return Path.home() / ".juicer" / "config.toml"


def _quote_toml_string(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\b", "\\b")
        .replace("\t", "\\t")
        .replace("\n", "\\n")
        .replace("\f", "\\f")
        .replace("\r", "\\r")
    )
    return f'"{escaped}"'


def _format_toml_value(value: str | int | bool) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return _quote_toml_string(value)


def _bank_to_toml(name: str, cfg: BankConfig) -> list[str]:
    lines = [f"[{name}]"]
    if cfg.action is not None:
        lines.append(f"action = {cfg.action.value}")
    lines.append(f"pre_delay_ms = {cfg.pre_delay_ms}")
    lines.append(f"post_delay_ms = {cfg.post_delay_ms}")
    return lines


def _sequence_to_toml(name: str, seq: SequenceConfig) -> list[str]:
    lines: list[str] = []
    for bank in range(1, 5):
        if lines:
            lines.append("")
        lines.extend(_bank_to_toml(f"{name}.bank{bank}", seq.bank(bank)))
    return lines


def dump_config_toml(config: GlobalConfig) -> str:
    lines = [
        f"port = {_format_toml_value(config.port)}",
        f"event_start_sound = {_format_toml_value(config.event_start_sound)}",
        f"event_stop_sound = {_format_toml_value(config.event_stop_sound)}",
        "",
    ]
    lines.extend(_sequence_to_toml("boot", config.boot))
    lines.append("")
    lines.append("")
    lines.extend(_sequence_to_toml("shutdown", config.shutdown))
    lines.append("")
    return "\n".join(lines)


# Backwards-compatible alias for callers still using the previous JSON name.
JsonStore = TomlStore
