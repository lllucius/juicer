from __future__ import annotations

import importlib
import logging
import os
import subprocess
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest


def _import_service_with_fake_pywin32() -> types.ModuleType:
    class ServiceFramework:
        def __init__(self, args: list[str]) -> None:
            self.statuses: list[tuple[int, int | None]] = []

        def ReportServiceStatus(self, status: int, waitHint: int | None = None) -> None:
            self.statuses.append((status, waitHint))

        def GetAcceptedControls(self) -> int:
            # Match pywin32's default: STOP plus SHUTDOWN when SvcShutdown exists.
            accepted = 0x00000001  # SERVICE_ACCEPT_STOP
            if hasattr(self, "SvcShutdown"):
                accepted |= 0x00000004  # SERVICE_ACCEPT_SHUTDOWN
            return accepted

        def SvcOtherEx(self, control: int, event_type: int, data: object) -> None:
            # Default: ignore unknown controls (matches pywin32's SvcOther fallback).
            pass

    servicemanager = types.SimpleNamespace(
        LogInfoMsg=lambda message: None,
        LogErrorMsg=lambda message: None,
        LogWarningMsg=lambda message: None,
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
        SERVICE_CONTROL_PRESHUTDOWN=15,
        SERVICE_ACCEPT_PRESHUTDOWN=0x100,
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

    sound_flags: list[bool] = []

    def run_boot(*args: object, **kwargs: object) -> None:
        sound_flags.append(cast(bool, kwargs["play_event_sounds"]))
        logging.getLogger("juicer.sequence").info("boot sequence detail")

    def run_shutdown(*args: object, **kwargs: object) -> None:
        sound_flags.append(cast(bool, kwargs["play_event_sounds"]))
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
    assert sound_flags == [False, False]


def test_play_configured_event_sound_uses_requested_sequence_sound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import juicer.config as config_module
    import juicer.sequence as sequence_module
    import juicer.service as service_module

    sounds: list[str] = []

    class FakeStore:
        def load(self) -> config_module.GlobalConfig:
            return config_module.GlobalConfig(
                boot=config_module.SequenceConfig(event_stop_sound="startup-complete.wav"),
                shutdown=config_module.SequenceConfig(event_start_sound="shutdown-start.wav"),
            )

    monkeypatch.setattr(config_module, "TomlStore", FakeStore)
    monkeypatch.setattr(sequence_module, "play_sound", sounds.append)

    service_module._play_configured_event_sound("boot", "event_stop_sound")
    service_module._play_configured_event_sound("shutdown", "event_start_sound")

    assert sounds == ["startup-complete.wav", "shutdown-start.wav"]


def test_svc_do_run_warns_when_service_log_cannot_be_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_module = _import_service_with_fake_pywin32()
    warnings: list[str] = []

    service_module._run_boot_sequence = lambda **kwargs: None
    service_module._run_shutdown_sequence = lambda: None
    monkeypatch.setattr(service_module, "_install_service_log_handler", lambda name: None)
    monkeypatch.setattr(service_module.servicemanager, "LogWarningMsg", warnings.append)

    service = service_module.JuicerService([])
    service.SvcDoRun()

    assert warnings == ["Juicer: Unable to open service.log for writing"]


def test_service_accepts_preshutdown_control() -> None:
    service_module = _import_service_with_fake_pywin32()
    service = service_module.JuicerService([])

    accepted = service.GetAcceptedControls()

    # PRESHUTDOWN must be advertised in addition to the framework defaults
    # (STOP + SHUTDOWN) so the SCM sends SERVICE_CONTROL_PRESHUTDOWN and
    # honours the longer PreshutdownTimeout instead of killing the service
    # after WaitToKillServiceTimeout (5 s by default).
    assert accepted & service_module.win32service.SERVICE_ACCEPT_PRESHUTDOWN
    assert accepted & 0x00000001  # STOP — framework default
    assert accepted & 0x00000004  # SHUTDOWN — framework default


def test_preshutdown_runs_shutdown_sequence_with_status_reporting() -> None:
    service_module = _import_service_with_fake_pywin32()
    win32service = service_module.win32service

    calls: list[str] = []
    set_event_calls: list[object] = []
    service_module.win32event.SetEvent = set_event_calls.append  # type: ignore[assignment]

    def fake_shutdown() -> None:
        calls.append("shutdown")

    service_module._run_shutdown_sequence = fake_shutdown

    service = service_module.JuicerService([])
    service.SvcOtherEx(win32service.SERVICE_CONTROL_PRESHUTDOWN, 0, None)

    assert calls == ["shutdown"]
    assert service._shutdown_done is True
    # The control handler reports STOP_PENDING at least once with a non-zero
    # waitHint so the SCM keeps waiting under PreshutdownTimeout.
    stop_pending = [
        (status, hint)
        for status, hint in service.statuses
        if status == win32service.SERVICE_STOP_PENDING
    ]
    assert stop_pending, service.statuses
    assert all(hint and hint > 0 for _, hint in stop_pending)
    # The stop event is signalled so SvcDoRun wakes up and finishes.
    assert set_event_calls, "stop event must be signalled after preshutdown"


def test_svc_other_ex_falls_back_for_unknown_controls() -> None:
    service_module = _import_service_with_fake_pywin32()
    service = service_module.JuicerService([])

    def must_not_run() -> None:
        raise AssertionError("shutdown must not run for unrelated controls")

    # Unknown control codes must not invoke the shutdown handler.
    service_module._run_shutdown_sequence = must_not_run
    service.SvcOtherEx(0xDEAD, 0, None)
    assert service._shutdown_done is False


def test_shutdown_with_status_reporting_keeps_reporting_during_slow_work() -> None:
    import threading as _threading

    service_module = _import_service_with_fake_pywin32()
    win32service = service_module.win32service

    started = _threading.Event()
    finish = _threading.Event()

    def slow_shutdown() -> None:
        started.set()
        # Block long enough that the poll loop must report STOP_PENDING
        # at least twice before the worker finishes.
        assert finish.wait(timeout=5.0)

    service_module._run_shutdown_sequence = slow_shutdown
    # Use a short interval so the test stays fast.
    service_module.SHUTDOWN_STATUS_INTERVAL_SEC = 0.05

    service = service_module.JuicerService([])

    def driver() -> None:
        service_module._run_shutdown_with_status_reporting(service)

    thread = _threading.Thread(target=driver)
    thread.start()
    try:
        assert started.wait(timeout=2.0)
        # Give the poll loop a chance to emit multiple status reports.
        import time

        time.sleep(0.25)
    finally:
        finish.set()
        thread.join(timeout=5.0)
    assert not thread.is_alive()

    stop_pending = [
        hint
        for status, hint in service.statuses
        if status == win32service.SERVICE_STOP_PENDING
    ]
    # One initial report + at least one polling-loop report.
    assert len(stop_pending) >= 2, service.statuses


def test_shutdown_with_status_reporting_creates_log_file_eagerly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """shutdown.log must exist on disk before the worker starts so a
    record survives even if the process is force-terminated mid-sequence.
    """
    service_module = _import_service_with_fake_pywin32()

    monkeypatch.setattr(
        service_module, "_service_log_path", lambda name: tmp_path / f"{name}.log"
    )

    log_file_existed_when_worker_started: list[bool] = []

    def fake_shutdown() -> None:
        log_file_existed_when_worker_started.append(
            (tmp_path / "shutdown.log").is_file()
        )

    service_module._run_shutdown_sequence = fake_shutdown
    service_module.SHUTDOWN_STATUS_INTERVAL_SEC = 0.01

    service = service_module.JuicerService([])
    service_module._run_shutdown_with_status_reporting(service)

    assert log_file_existed_when_worker_started == [True]
    # The eagerly installed handler logs a "Shutdown control received" line.
    contents = (tmp_path / "shutdown.log").read_text(encoding="utf-8")
    assert "Shutdown control received" in contents


def test_shutdown_with_status_reporting_reraises_worker_exception() -> None:
    service_module = _import_service_with_fake_pywin32()

    def failing_shutdown() -> None:
        raise RuntimeError("boom")

    service_module._run_shutdown_sequence = failing_shutdown
    service_module.SHUTDOWN_STATUS_INTERVAL_SEC = 0.01

    service = service_module.JuicerService([])
    with pytest.raises(RuntimeError, match="boom"):
        service_module._run_shutdown_with_status_reporting(service)


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


def test_install_service_uses_bundled_service_executable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service_module = _import_service_with_fake_pywin32()
    service_exe = tmp_path / "juicer_service.exe"
    service_exe.write_bytes(b"")
    installed_kwargs: dict[str, object] = {}

    def install_service(**kwargs: object) -> None:
        installed_kwargs.update(kwargs)

    monkeypatch.setattr(service_module, "_is_user_admin", lambda: True)
    monkeypatch.setattr(service_module, "_find_bundled_service_exe", lambda: str(service_exe))
    monkeypatch.setattr(service_module.win32serviceutil, "InstallService", install_service)

    service_module.install_service()

    assert installed_kwargs["exeName"] == str(service_exe)
    assert "pythonClassString" not in installed_kwargs


def test_install_service_requires_bundled_service_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_module = _import_service_with_fake_pywin32()

    monkeypatch.setattr(service_module, "_is_user_admin", lambda: True)
    monkeypatch.setattr(service_module, "_find_bundled_service_exe", lambda: None)

    with pytest.raises(OSError) as info:
        service_module.install_service()

    assert "juicer_service.exe was not found" in str(info.value)


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
    tmp_path: Path,
) -> None:
    service_module = _import_service_with_fake_pywin32()
    service_exe = tmp_path / "juicer_service.exe"
    service_exe.write_bytes(b"")
    installed_kwargs: dict[str, object] = {}

    def install_service(**kwargs: object) -> None:
        installed_kwargs.update(kwargs)

    def fail_elevation(_: str) -> None:
        raise AssertionError("unexpected elevation request")

    monkeypatch.setattr(service_module, "_is_user_admin", lambda: False)
    monkeypatch.setattr(service_module, "_find_bundled_service_exe", lambda: str(service_exe))
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


def test_elevated_service_command_runs_from_importable_package_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_module = _import_service_with_fake_pywin32()
    captured: dict[str, object] = {}

    class ShellExecuteEx:
        argtypes: object = None
        restype: object = None

        def __call__(self, pointer: object) -> bool:
            sei = pointer._obj
            captured["lpDirectory"] = sei.lpDirectory
            captured["lpParameters"] = sei.lpParameters
            sei.hProcess = 100
            return True

    class FakeKernel32:
        def WaitForSingleObject(self, handle: object, timeout: object) -> int:
            return 0

        def GetExitCodeProcess(self, handle: object, pointer: object) -> bool:
            pointer._obj.value = 0
            return True

        def CloseHandle(self, handle: object) -> bool:
            return True

    fake_shell32 = types.SimpleNamespace(ShellExecuteExW=ShellExecuteEx())
    fake_kernel32 = FakeKernel32()

    def windows_dll(name: str) -> object | None:
        return {"shell32": fake_shell32, "kernel32": fake_kernel32}.get(name)

    monkeypatch.setattr(service_module, "_windows_dll", windows_dll)

    service_module._request_elevated_service_command("install")

    assert captured["lpDirectory"] == service_module._service_package_parent()
    params = str(captured["lpParameters"])
    assert "--elevated-service-command install" in params
    assert "--elevated-log-path" in params


def test_elevated_service_command_failure_includes_subprocess_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Subprocess stderr captured to the log file must surface in the OSError."""
    service_module = _import_service_with_fake_pywin32()

    captured_log_path: dict[str, str] = {}

    class ShellExecuteEx:
        argtypes: object = None
        restype: object = None

        def __call__(self, pointer: object) -> bool:
            sei = pointer._obj
            params = str(sei.lpParameters)
            tokens = params.split()
            idx = tokens.index("--elevated-log-path")
            log_path = tokens[idx + 1].strip('"')
            captured_log_path["path"] = log_path
            Path(log_path).write_text(
                "Traceback (most recent call last):\n  ModuleNotFoundError: juicer\n",
                encoding="utf-8",
            )
            sei.hProcess = 100
            return True

    class FakeKernel32:
        def WaitForSingleObject(self, handle: object, timeout: object) -> int:
            return 0

        def GetExitCodeProcess(self, handle: object, pointer: object) -> bool:
            pointer._obj.value = 1
            return True

        def CloseHandle(self, handle: object) -> bool:
            return True

    fake_shell32 = types.SimpleNamespace(ShellExecuteExW=ShellExecuteEx())
    fake_kernel32 = FakeKernel32()

    monkeypatch.setattr(
        service_module,
        "_windows_dll",
        lambda name: {"shell32": fake_shell32, "kernel32": fake_kernel32}.get(name),
    )

    with pytest.raises(OSError) as info:
        service_module._request_elevated_service_command("install")

    message = str(info.value)
    assert "exit code 1" in message
    assert "ModuleNotFoundError" in message
    # Temp log file should be removed on completion.
    assert not Path(captured_log_path["path"]).exists()


def test_elevated_service_command_failure_without_subprocess_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the subprocess writes nothing, the OSError mentions that fact."""
    service_module = _import_service_with_fake_pywin32()

    class ShellExecuteEx:
        argtypes: object = None
        restype: object = None

        def __call__(self, pointer: object) -> bool:
            pointer._obj.hProcess = 100
            return True

    class FakeKernel32:
        def WaitForSingleObject(self, handle: object, timeout: object) -> int:
            return 0

        def GetExitCodeProcess(self, handle: object, pointer: object) -> bool:
            pointer._obj.value = 1
            return True

        def CloseHandle(self, handle: object) -> bool:
            return True

    fake_shell32 = types.SimpleNamespace(ShellExecuteExW=ShellExecuteEx())
    fake_kernel32 = FakeKernel32()
    monkeypatch.setattr(
        service_module,
        "_windows_dll",
        lambda name: {"shell32": fake_shell32, "kernel32": fake_kernel32}.get(name),
    )

    with pytest.raises(OSError) as info:
        service_module._request_elevated_service_command("install")

    assert "no output was captured" in str(info.value)


def test_run_elevated_command_with_logging_writes_traceback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The elevated subprocess must capture exceptions to the shared log file."""
    service_module = _import_service_with_fake_pywin32()
    log_path = tmp_path / "elevated.log"

    def boom(command: str) -> None:
        raise RuntimeError("install exploded for testing")

    monkeypatch.setattr(service_module, "_run_service_command_without_elevation", boom)

    code = service_module._run_elevated_command_with_logging("install", str(log_path))

    assert code == 1
    contents = log_path.read_text(encoding="utf-8")
    assert "install exploded for testing" in contents
    assert "Traceback" in contents


def test_run_elevated_command_with_logging_returns_zero_on_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service_module = _import_service_with_fake_pywin32()
    log_path = tmp_path / "elevated.log"

    monkeypatch.setattr(
        service_module,
        "_run_service_command_without_elevation",
        lambda command: None,
    )

    code = service_module._run_elevated_command_with_logging("install", str(log_path))

    assert code == 0
    assert not log_path.exists()


def test_service_log_path_falls_back_when_config_import_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A broken config must not prevent service.log from being written."""
    import juicer.service as service_module

    monkeypatch.setattr(
        service_module,
        "_default_service_log_dir",
        lambda: tmp_path / "fallback-juicer",
    )
    # Force the inner import to raise by shadowing juicer.config with a broken
    # module that raises whenever TomlStore is constructed.
    broken = types.ModuleType("juicer.config")

    def broken_tomlstore(*args: object, **kwargs: object) -> object:
        raise RuntimeError("config import broken")

    broken.TomlStore = broken_tomlstore  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "juicer.config", broken)

    path = service_module._service_log_path("service")

    assert path == tmp_path / "fallback-juicer" / "service.log"


def test_install_service_log_handler_creates_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The early service log handler must create the log file on disk."""
    import juicer.service as service_module

    target = tmp_path / "logs" / "service.log"
    monkeypatch.setattr(service_module, "_service_log_path", lambda name: target)

    installed = service_module._install_service_log_handler("service")

    assert installed is not None
    handler, path = installed
    try:
        logging.getLogger().info("hello from the service")
        handler.flush()
        assert path == target
        assert target.is_file()
        assert "hello from the service" in target.read_text(encoding="utf-8")
    finally:
        service_module._remove_service_log_handler(handler)


def test_start_service_failure_surfaces_diagnostic_hints(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Start failures must point users at the log files and Event Viewer."""
    service_module = _import_service_with_fake_pywin32()

    def boom(name: str) -> None:
        raise RuntimeError("SCM timeout")

    monkeypatch.setattr(service_module.win32serviceutil, "StartService", boom)
    monkeypatch.setattr(service_module, "_is_user_admin", lambda: True)
    monkeypatch.setattr(
        service_module,
        "_service_log_path",
        lambda name: tmp_path / f"{name}.log",
    )

    with pytest.raises(OSError) as info:
        service_module.start_service()

    message = str(info.value)
    assert "SCM timeout" in message
    assert str(tmp_path / "service.log") in message
    assert "Event Viewer" in message
