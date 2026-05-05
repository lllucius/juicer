from __future__ import annotations

import importlib
import logging
import os
import subprocess
import sys
import types
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import pytest


def _import_service_with_fake_pywin32() -> types.ModuleType:
    class ServiceFramework:
        def __init__(self, args: list[str]) -> None:
            self.statuses: list[tuple[int, int | None]] = []

        def ReportServiceStatus(self, status: int, waitHint: int | None = None) -> None:
            self.statuses.append((status, waitHint))

    servicemanager = types.SimpleNamespace(
        LogInfoMsg=lambda message: None,
        LogErrorMsg=lambda message: None,
    )
    win32event = types.SimpleNamespace(
        CreateEvent=lambda *args: object(),
        WaitForSingleObject=lambda *args: 0,
        SetEvent=lambda event: None,
        INFINITE=-1,
    )
    win32service = types.SimpleNamespace(
        SERVICE_START_PENDING=2,
        SERVICE_STOP_PENDING=3,
        SERVICE_RUNNING=4,
        SERVICE_STOPPED=1,
        SERVICE_CONTINUE_PENDING=5,
        SERVICE_PAUSE_PENDING=6,
        SERVICE_PAUSED=7,
        SERVICE_AUTO_START=2,
    )
    win32serviceutil = types.SimpleNamespace(
        ServiceFramework=ServiceFramework,
        InstallService=lambda **kwargs: None,
        RemoveService=lambda name: None,
        StartService=lambda name: None,
        StopService=lambda name: None,
        RestartService=lambda name: None,
        QueryServiceStatus=lambda name: (None, win32service.SERVICE_RUNNING),
        HandleCommandLine=lambda service_class: None,
    )

    modules = {
        "servicemanager": servicemanager,
        "win32event": win32event,
        "win32service": win32service,
        "win32serviceutil": win32serviceutil,
    }
    sys.modules.pop("juicer.service", None)
    with patch("platform.system", return_value="Windows"), patch.dict(sys.modules, modules):
        return importlib.import_module("juicer.service")


def test_svc_do_run_reports_running_only_after_boot_completion() -> None:
    service_module = _import_service_with_fake_pywin32()
    win32service = service_module.win32service
    holder: dict[str, object] = {}
    boot_completed = False

    def boot(
        progress_callback: Callable[[], None] | None = None,
        cancel: object | None = None,
    ) -> None:
        nonlocal boot_completed
        assert cancel is not None
        service = holder["service"]
        assert win32service.SERVICE_RUNNING not in [status for status, _ in service.statuses]
        assert progress_callback is not None
        progress_callback()
        assert win32service.SERVICE_RUNNING not in [status for status, _ in service.statuses]
        boot_completed = True

    service_module._run_boot_sequence = boot
    service_module._run_shutdown_sequence = lambda: None

    service = service_module.JuicerService([])
    holder["service"] = service
    service.SvcDoRun()

    statuses = [status for status, _ in service.statuses]
    assert boot_completed
    assert statuses[:2] == [
        win32service.SERVICE_START_PENDING,
        win32service.SERVICE_START_PENDING,
    ]
    assert win32service.SERVICE_RUNNING in statuses
    assert statuses.index(win32service.SERVICE_RUNNING) > 1
    assert statuses[-1] == win32service.SERVICE_STOPPED


def test_run_boot_sequence_closes_transport_when_progress_callback_after_open_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import juicer.config as config_module
    import juicer.protocol as protocol_module
    import juicer.service as service_module

    opened: list[object] = []
    closed: list[object] = []

    class FakeStore:
        path = tmp_path / "config.toml"

        def load(self) -> config_module.GlobalConfig:
            return config_module.GlobalConfig(port="COM1")

    class FakeTransport:
        def __init__(self, port: str) -> None:
            self.port = port

        def open(self) -> None:
            opened.append(self)

        def close(self) -> None:
            closed.append(self)

    class FakeClient:
        def __init__(self, transport: FakeTransport) -> None:
            self.transport = transport

    progress_calls = 0

    def progress_callback() -> None:
        nonlocal progress_calls
        progress_calls += 1
        if progress_calls == 2:
            raise RuntimeError("startup status failed")

    monkeypatch.setattr(config_module, "TomlStore", FakeStore)
    monkeypatch.setattr(protocol_module, "SerialTransport", FakeTransport)
    monkeypatch.setattr(protocol_module, "JuicerClient", FakeClient)

    with pytest.raises(RuntimeError, match="startup status failed"):
        service_module._run_boot_sequence(progress_callback=progress_callback)

    assert len(opened) == 1
    assert closed == opened


def test_service_boot_and_shutdown_logs_are_written_next_to_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import juicer.config as config_module
    import juicer.protocol as protocol_module
    import juicer.sequence as sequence_module
    import juicer.service as service_module

    config_path = tmp_path / "config.toml"

    class FakeStore:
        path = config_path

        def load(self) -> config_module.GlobalConfig:
            return config_module.GlobalConfig(port="COM1")

    class FakeTransport:
        def __init__(self, port: str) -> None:
            self.port = port

        def open(self) -> None:
            pass

        def close(self) -> None:
            pass

    class FakeClient:
        def __init__(self, transport: FakeTransport) -> None:
            self.transport = transport

    def run_boot(*args: object, **kwargs: object) -> None:
        logging.getLogger("juicer.sequence").info("boot sequence detail")

    def run_shutdown(*args: object, **kwargs: object) -> None:
        logging.getLogger("juicer.sequence").info("shutdown sequence detail")

    monkeypatch.setattr(config_module, "TomlStore", FakeStore)
    monkeypatch.setattr(protocol_module, "SerialTransport", FakeTransport)
    monkeypatch.setattr(protocol_module, "JuicerClient", FakeClient)
    monkeypatch.setattr(sequence_module, "run_boot", run_boot)
    monkeypatch.setattr(sequence_module, "run_shutdown", run_shutdown)

    service_module._run_boot_sequence()
    service_module._run_shutdown_sequence()

    assert (tmp_path / "boot.log").is_file()
    assert (tmp_path / "shutdown.log").is_file()
    assert "boot sequence detail" in (tmp_path / "boot.log").read_text(encoding="utf-8")
    assert "shutdown sequence detail" in (tmp_path / "shutdown.log").read_text(encoding="utf-8")


def test_cli_can_run_directly_from_source_directory() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    cli_dir = repo_root / "src" / "juicer"
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)

    result = subprocess.run(
        [sys.executable, "cli.py", "--help"],
        cwd=cli_dir,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "Juicer" in result.stdout


def test_install_service_uses_native_python_service_host(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service_module = _import_service_with_fake_pywin32()
    pythonservice_exe = tmp_path / "pythonservice.exe"
    pythonservice_exe.write_bytes(b"")
    installed_kwargs: dict[str, object] = {}

    def install_service(**kwargs: object) -> None:
        installed_kwargs.update(kwargs)

    monkeypatch.setattr(service_module, "_is_user_admin", lambda: True)
    monkeypatch.setattr(service_module, "_find_pythonservice_exe", lambda: str(pythonservice_exe))
    monkeypatch.setattr(service_module.win32serviceutil, "InstallService", install_service)

    service_module.install_service()

    assert installed_kwargs["exeName"] == str(pythonservice_exe)
    assert installed_kwargs["pythonClassString"] == "juicer.service.JuicerService"


def test_install_service_falls_back_to_pywin32_default_when_pythonservice_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_module = _import_service_with_fake_pywin32()
    installed_kwargs: dict[str, object] = {}

    def install_service(**kwargs: object) -> None:
        installed_kwargs.update(kwargs)

    monkeypatch.setattr(service_module, "_is_user_admin", lambda: True)
    monkeypatch.setattr(service_module, "_find_pythonservice_exe", lambda: None)
    monkeypatch.setattr(service_module.win32serviceutil, "InstallService", install_service)

    service_module.install_service()

    assert "exeName" not in installed_kwargs


def test_service_management_requests_elevation_when_not_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_module = _import_service_with_fake_pywin32()
    requested: list[str] = []

    def fail_install(**kwargs: object) -> None:
        raise AssertionError("InstallService should be delegated to an elevated process")

    monkeypatch.setattr(service_module, "_is_user_admin", lambda: False)
    monkeypatch.setattr(
        service_module,
        "_request_elevated_service_command",
        lambda command: requested.append(command),
    )
    monkeypatch.setattr(service_module.win32serviceutil, "InstallService", fail_install)

    service_module.install_service()

    assert requested == ["install"]


def test_admin_check_defaults_to_not_elevated_when_status_is_unavailable() -> None:
    service_module = _import_service_with_fake_pywin32()

    assert service_module._is_user_admin() is False


def test_service_management_can_skip_elevation_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_module = _import_service_with_fake_pywin32()
    installed_kwargs: dict[str, object] = {}

    def install_service(**kwargs: object) -> None:
        installed_kwargs.update(kwargs)

    def fail_elevation(_: str) -> None:
        raise AssertionError("unexpected elevation request")

    monkeypatch.setattr(service_module, "_is_user_admin", lambda: False)
    monkeypatch.setattr(service_module, "_request_elevated_service_command", fail_elevation)
    monkeypatch.setattr(service_module.win32serviceutil, "InstallService", install_service)

    service_module.install_service(elevate=False)

    assert installed_kwargs["serviceName"] == service_module.SERVICE_NAME


def test_elevated_service_command_dispatches_without_requesting_elevation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_module = _import_service_with_fake_pywin32()
    calls: list[bool] = []

    def install_service(*, elevate: bool = True) -> None:
        calls.append(elevate)

    monkeypatch.setattr(service_module, "install_service", install_service)

    service_module._run_service_command_without_elevation("install")

    assert calls == [False]
