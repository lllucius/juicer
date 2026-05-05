from __future__ import annotations

import importlib
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
) -> None:
    import juicer.config as config_module
    import juicer.protocol as protocol_module
    import juicer.service as service_module

    opened: list[object] = []
    closed: list[object] = []

    class FakeStore:
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

    monkeypatch.setattr(service_module, "_find_pythonservice_exe", lambda: None)
    monkeypatch.setattr(service_module.win32serviceutil, "InstallService", install_service)

    service_module.install_service()

    assert "exeName" not in installed_kwargs
