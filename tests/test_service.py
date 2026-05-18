from __future__ import annotations

import importlib
import logging
import ntpath
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
    assert installed_kwargs["pythonClassString"] == ntpath.join(
        service_module._service_package_parent(),
        "juicer.service.JuicerService",
    )


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


def test_compute_service_environment_includes_existing_sys_path_entries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """PYTHONPATH must list installer sys.path entries that exist on disk."""
    import juicer.service as service_module

    real_a = tmp_path / "site-a"
    real_b = tmp_path / "site-b"
    real_a.mkdir()
    real_b.mkdir()
    missing = tmp_path / "does-not-exist"

    # Include a duplicate (with different casing) to verify dedup.
    fake_path = [
        "",
        str(real_a),
        str(missing),
        str(real_b),
        str(real_a).upper() if os.name == "nt" else str(real_a),
    ]
    monkeypatch.setattr(service_module.sys, "path", fake_path)
    monkeypatch.setattr(service_module, "_running_in_venv", lambda: False)

    env = service_module._compute_service_environment()

    pythonpath = next((e for e in env if e.startswith("PYTHONPATH=")), None)
    assert pythonpath is not None
    entries = pythonpath.removeprefix("PYTHONPATH=").split(os.pathsep)
    assert str(real_a) in entries
    assert str(real_b) in entries
    assert str(missing) not in entries
    assert "" not in entries
    # No PYTHONHOME when not in a venv.
    assert not any(e.startswith("PYTHONHOME=") for e in env)


def test_compute_service_environment_exports_pythonhome_in_venv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """PYTHONHOME must be exported when the installer runs inside a venv."""
    import juicer.service as service_module

    venv = tmp_path / "venv"
    venv.mkdir()
    monkeypatch.setattr(service_module.sys, "prefix", str(venv))
    monkeypatch.setattr(service_module.sys, "base_prefix", str(tmp_path / "system"))
    # Ensure at least one valid path entry exists so PYTHONPATH is also emitted.
    monkeypatch.setattr(service_module.sys, "path", [str(venv)])

    env = service_module._compute_service_environment()

    assert f"PYTHONHOME={venv}" in env


def test_compute_service_environment_skips_pythonhome_outside_venv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import juicer.service as service_module

    monkeypatch.setattr(service_module.sys, "prefix", str(tmp_path))
    monkeypatch.setattr(service_module.sys, "base_prefix", str(tmp_path))
    monkeypatch.setattr(service_module.sys, "path", [str(tmp_path)])

    env = service_module._compute_service_environment()

    assert not any(e.startswith("PYTHONHOME=") for e in env)


def test_write_service_environment_writes_reg_multi_sz(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The registry helper must write the Environment value as REG_MULTI_SZ."""
    import juicer.service as service_module

    captured: dict[str, object] = {}

    class FakeKey:
        def __enter__(self) -> "FakeKey":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    def fake_open_key(
        root: object, path: str, reserved: int, access: int
    ) -> FakeKey:
        captured["root"] = root
        captured["path"] = path
        captured["access"] = access
        return FakeKey()

    def fake_set_value(
        key: FakeKey, name: str, reserved: int, vtype: int, value: object
    ) -> None:
        captured["name"] = name
        captured["vtype"] = vtype
        captured["value"] = value

    fake_winreg = types.SimpleNamespace(
        OpenKey=fake_open_key,
        SetValueEx=fake_set_value,
        HKEY_LOCAL_MACHINE="HKLM",
        KEY_SET_VALUE=0x0002,
        REG_MULTI_SZ=7,
    )
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)

    service_module._write_service_environment(
        ["PYTHONPATH=C:\\a;C:\\b", "PYTHONHOME=C:\\venv"]
    )

    assert captured["root"] == "HKLM"
    assert captured["path"] == r"SYSTEM\CurrentControlSet\Services\Juicer"
    assert captured["access"] == 0x0002
    assert captured["name"] == "Environment"
    assert captured["vtype"] == 7
    assert captured["value"] == [
        "PYTHONPATH=C:\\a;C:\\b",
        "PYTHONHOME=C:\\venv",
    ]


def test_write_service_environment_is_noop_when_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import juicer.service as service_module

    def boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("winreg should not be touched when env is empty")

    fake_winreg = types.SimpleNamespace(
        OpenKey=boom,
        SetValueEx=boom,
        HKEY_LOCAL_MACHINE=None,
        KEY_SET_VALUE=0,
        REG_MULTI_SZ=0,
    )
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)

    service_module._write_service_environment([])


def test_write_service_environment_tolerates_oserror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing registry write must not crash install_service."""
    import juicer.service as service_module

    def raising_open(*args: object, **kwargs: object) -> object:
        raise OSError("access denied")

    fake_winreg = types.SimpleNamespace(
        OpenKey=raising_open,
        SetValueEx=lambda *a, **k: None,
        HKEY_LOCAL_MACHINE=None,
        KEY_SET_VALUE=0,
        REG_MULTI_SZ=0,
    )
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)

    # Should not raise.
    service_module._write_service_environment(["PYTHONPATH=C:\\x"])


def test_install_service_writes_service_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """install_service must propagate the venv sys.path into the registry."""
    service_module = _import_service_with_fake_pywin32()

    monkeypatch.setattr(service_module, "_is_user_admin", lambda: True)
    monkeypatch.setattr(service_module, "_find_pythonservice_exe", lambda: None)
    monkeypatch.setattr(
        service_module.win32serviceutil, "InstallService", lambda **kw: None
    )

    computed = ["PYTHONPATH=C:\\venv\\Lib\\site-packages", "PYTHONHOME=C:\\venv"]
    monkeypatch.setattr(service_module, "_compute_service_environment", lambda: computed)

    written: list[list[str]] = []
    monkeypatch.setattr(
        service_module,
        "_write_service_environment",
        lambda env: written.append(env),
    )

    service_module.install_service()

    assert written == [computed]


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
