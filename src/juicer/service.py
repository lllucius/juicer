"""Juicer Windows service — pywin32 ServiceFramework wrapper.

Service name: ``Juicer``
Display name: ``Juicer UPS Controller``
Auto-start, depends on Serenum + Serial.

Importable on any OS; runtime methods raise ``OSError`` on non-Windows.
"""

from __future__ import annotations

import ctypes
import logging
import os
import platform
import site
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Literal, cast

logger = logging.getLogger(__name__)

_WINDOWS = platform.system() == "Windows"

SEE_MASK_NOCLOSEPROCESS = 0x00000040
SW_SHOWNORMAL = 1
INFINITE = 0xFFFFFFFF


class SHELLEXECUTEINFO(ctypes.Structure):
    """ctypes mirror of the Win32 SHELLEXECUTEINFO structure."""

    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("fMask", ctypes.c_ulong),
        ("hwnd", ctypes.c_void_p),
        ("lpVerb", ctypes.c_wchar_p),
        ("lpFile", ctypes.c_wchar_p),
        ("lpParameters", ctypes.c_wchar_p),
        ("lpDirectory", ctypes.c_wchar_p),
        ("nShow", ctypes.c_int),
        ("hInstApp", ctypes.c_void_p),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", ctypes.c_wchar_p),
        ("hkeyClass", ctypes.c_void_p),
        ("dwHotKey", ctypes.c_ulong),
        ("hIcon", ctypes.c_void_p),
        ("hProcess", ctypes.c_void_p),
    ]

# Service constants (matching C++ svc.cpp)
SERVICE_NAME = "Juicer"
SERVICE_DISPLAY_NAME = "Juicer UPS Controller"
SERVICE_DESCRIPTION = "Controls Furman F1500-UPS E outlet banks via RS-232 serial"
SERVICE_DEPS = ["Serenum", "Serial"]

# ──────────────────────────────────────────────────────────────────────
# Service Framework (Windows-only at runtime)
# ──────────────────────────────────────────────────────────────────────

if _WINDOWS:
    try:
        import servicemanager
        import win32event
        import win32service
        import win32serviceutil

        _PYWIN32_AVAILABLE = True
    except ImportError:
        _PYWIN32_AVAILABLE = False
else:
    _PYWIN32_AVAILABLE = False


def _ensure_pywin32() -> None:
    if not _PYWIN32_AVAILABLE:
        raise OSError(
            "pywin32 is required for service operations. "
            "Install with: pip install pywin32"
        )


ProgressCallback = Callable[[], None]
ServiceCommand = Literal["install", "uninstall", "start", "stop", "restart"]
_SERVICE_COMMANDS: frozenset[str] = frozenset(("install", "uninstall", "start", "stop", "restart"))


def _windows_dll(name: str) -> Any | None:
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return None
    return getattr(windll, name, None)


def _is_user_admin() -> bool:
    """Return whether the current Windows process is elevated."""
    if not _WINDOWS:
        return False
    shell32 = _windows_dll("shell32")
    if shell32 is None:
        return False
    try:
        return bool(shell32.IsUserAnAdmin())
    except (AttributeError, OSError, TypeError):
        return False


def _request_elevated_service_command(command: ServiceCommand) -> None:
    """Run a service management command through Windows UAC and wait for it."""
    if command not in _SERVICE_COMMANDS:
        raise ValueError(f"Unsupported elevated service command: {command}")

    shell32 = _windows_dll("shell32")
    kernel32 = _windows_dll("kernel32")
    if shell32 is None or kernel32 is None:
        raise OSError("Unable to request elevated privileges on this platform")

    params = subprocess.list2cmdline(
        ["-m", "juicer.service", "--elevated-service-command", command]
    )
    shell_execute_ex = shell32.ShellExecuteExW
    shell_execute_ex.argtypes = [ctypes.POINTER(SHELLEXECUTEINFO)]
    shell_execute_ex.restype = ctypes.c_bool

    sei = SHELLEXECUTEINFO()
    sei.cbSize = ctypes.sizeof(SHELLEXECUTEINFO)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS
    sei.lpVerb = "runas"
    sei.lpFile = sys.executable
    sei.lpParameters = params
    sei.nShow = SW_SHOWNORMAL

    if not shell_execute_ex(ctypes.byref(sei)):
        raise OSError("Windows elevation request was cancelled or failed")

    try:
        kernel32.WaitForSingleObject(sei.hProcess, INFINITE)
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(sei.hProcess, ctypes.byref(exit_code)):
            raise OSError("Unable to read elevated process exit code")
        if exit_code.value != 0:
            raise OSError(
                f"Elevated service {command} failed with exit code {exit_code.value}; "
                "run the command from an elevated console for details"
            )
    finally:
        kernel32.CloseHandle(sei.hProcess)


def _request_elevation_if_needed(command: ServiceCommand, elevate: bool) -> bool:
    """Request UAC elevation when needed; return whether the caller should continue."""
    if _WINDOWS and elevate and not _is_user_admin():
        _request_elevated_service_command(command)
        return False
    return True


def _service_log_path(name: str) -> Path:
    """Return a service log path next to the TOML configuration."""
    from juicer.config import TomlStore

    config_path = TomlStore().path
    return config_path.with_name(f"{name}.log")


@contextmanager
def _service_file_logging(name: str) -> Iterator[Path]:
    """Temporarily route service Python logging to a sequence-specific file."""
    path = _service_log_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    previous_level = root_logger.level
    if previous_level > logging.INFO:
        root_logger.setLevel(logging.INFO)
    try:
        logger.info("Writing %s service log to %s", name, path)
        yield path
    finally:
        logger.info("Finished writing %s service log to %s", name, path)
        root_logger.removeHandler(handler)
        root_logger.setLevel(previous_level)
        handler.close()


class _Win32CancelToken:
    """Cancellation token backed by a Win32 event handle."""

    def __init__(self, event: object) -> None:
        self._event = event

    def is_set(self) -> bool:
        _ensure_pywin32()
        return cast(bool, win32event.WaitForSingleObject(self._event, 0) == 0)


def _run_boot_sequence(
    progress_callback: ProgressCallback | None = None,
    cancel: _Win32CancelToken | None = None,
) -> None:
    """Load config from TOML and run boot sequence."""
    from juicer.config import TomlStore
    from juicer.protocol import JuicerClient, SerialTransport
    from juicer.sequence import run_boot

    with _service_file_logging("boot"):
        store = TomlStore()
        config = store.load()

        transport = SerialTransport(port=config.port)
        if progress_callback is not None:
            progress_callback()
        transport.open()
        try:
            if progress_callback is not None:
                progress_callback()
            client = JuicerClient(transport)
            run_boot(config, client, progress_callback=progress_callback, cancel=cancel)
        finally:
            transport.close()


def _run_shutdown_sequence() -> None:
    """Load config from TOML and run shutdown sequence."""
    from juicer.config import TomlStore
    from juicer.protocol import JuicerClient, SerialTransport
    from juicer.sequence import run_shutdown

    with _service_file_logging("shutdown"):
        store = TomlStore()
        config = store.load()

        transport = SerialTransport(port=config.port)
        transport.open()
        try:
            client = JuicerClient(transport)
            run_shutdown(config, client)
        finally:
            transport.close()


# ── Conditional class definition to allow import on any OS ────────────

if _PYWIN32_AVAILABLE:

    class JuicerService(win32serviceutil.ServiceFramework):  # type: ignore[misc]
        """Windows service that runs boot on start and shutdown on stop.

        Mirrors the C++ ``svc.cpp`` lifecycle:
        - SvcDoRun → powerup() → wait → powerdown()
        - SERVICE_CONTROL_STOP → signal stop event
        - SERVICE_CONTROL_SHUTDOWN → powerdown() directly + signal stop
        """

        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = SERVICE_DISPLAY_NAME
        _svc_description_ = SERVICE_DESCRIPTION
        _svc_deps_ = SERVICE_DEPS

        def __init__(self, args: list[str]) -> None:
            win32serviceutil.ServiceFramework.__init__(self, args)
            self.stop_event = win32event.CreateEvent(None, True, False, None)
            self._shutdown_done = False
            self._reporting_start_pending = False

        def _report_start_pending(self) -> None:
            """Report synchronous startup progress to the SCM."""
            if self._reporting_start_pending:
                self.ReportServiceStatus(
                    win32service.SERVICE_START_PENDING,
                    waitHint=60000,
                )

        def SvcDoRun(self) -> None:
            """Main service entry: boot → wait → shutdown.

            Startup intentionally remains synchronous: ``SERVICE_RUNNING`` is
            reported only after outlet boot has completed, while periodic
            ``SERVICE_START_PENDING`` updates keep the SCM informed.
            """
            try:
                self._reporting_start_pending = True
                self._report_start_pending()
                servicemanager.LogInfoMsg(f"{SERVICE_NAME}: Running boot sequence")

                try:
                    _run_boot_sequence(
                        progress_callback=self._report_start_pending,
                        cancel=_Win32CancelToken(self.stop_event),
                    )
                except Exception as exc:
                    servicemanager.LogErrorMsg(f"{SERVICE_NAME}: Boot failed: {exc}")
                    logger.error("Boot sequence failed: %s", exc)
                    return

                self._reporting_start_pending = False
                self.ReportServiceStatus(win32service.SERVICE_RUNNING)
                servicemanager.LogInfoMsg(f"{SERVICE_NAME}: Service running")

                # Block until stop event is signalled
                win32event.WaitForSingleObject(self.stop_event, win32event.INFINITE)

                # Run shutdown sequence after stop event unless SvcShutdown already did it.
                if not self._shutdown_done:
                    servicemanager.LogInfoMsg(f"{SERVICE_NAME}: Running shutdown sequence")
                    try:
                        _run_shutdown_sequence()
                    except Exception as exc:
                        servicemanager.LogErrorMsg(f"{SERVICE_NAME}: Shutdown failed: {exc}")
                        logger.error("Shutdown sequence failed: %s", exc)

            finally:
                self.ReportServiceStatus(win32service.SERVICE_STOPPED)
                servicemanager.LogInfoMsg(f"{SERVICE_NAME}: Service stopped")

        def SvcStop(self) -> None:
            """Handle SERVICE_CONTROL_STOP."""
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING, waitHint=60000)
            servicemanager.LogInfoMsg(f"{SERVICE_NAME}: Stop requested")
            win32event.SetEvent(self.stop_event)

        def SvcShutdown(self) -> None:
            """Handle SERVICE_CONTROL_SHUTDOWN — run shutdown directly.

            On system shutdown there is limited time, so we run powerdown()
            directly in the handler (matching C++ behaviour).
            """
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING, waitHint=60000)
            servicemanager.LogInfoMsg(f"{SERVICE_NAME}: System shutdown — running shutdown now")
            self._shutdown_done = True
            try:
                _run_shutdown_sequence()
            except Exception as exc:
                servicemanager.LogErrorMsg(f"{SERVICE_NAME}: Shutdown failed: {exc}")
            finally:
                win32event.SetEvent(self.stop_event)

else:

    class JuicerService:  # type: ignore[no-redef]
        """Placeholder for non-Windows systems."""

        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = SERVICE_DISPLAY_NAME

        def __init__(self, *args: object, **kwargs: object) -> None:
            raise OSError("JuicerService requires Windows + pywin32")


# ──────────────────────────────────────────────────────────────────────
# Service management functions
# ──────────────────────────────────────────────────────────────────────


def _find_pythonservice_exe() -> str | None:
    """Return the path to pythonservice.exe in its existing site-packages location.

    pywin32 normally tries to move pythonservice.exe next to the Python interpreter
    so that the Service Control Manager can find it.  On Windows Store Python
    installations that target directory is read-only, so the move fails with
    "Access is denied."  Passing the already-installed path directly as ``exeName``
    to :func:`win32serviceutil.InstallService` skips the relocation step entirely.
    """
    search_dirs: list[str] = [os.path.join(sys.prefix, "Lib", "site-packages")]
    if hasattr(site, "getsitepackages"):
        search_dirs.extend(site.getsitepackages())
    user_site = site.getusersitepackages()
    if user_site:
        search_dirs.append(user_site)

    for site_dir in search_dirs:
        candidate = os.path.join(site_dir, "win32", "pythonservice.exe")
        if os.path.isfile(candidate):
            return candidate
    return None


def install_service(*, elevate: bool = True) -> None:
    """Install the Juicer Windows service."""
    _ensure_pywin32()
    if not _request_elevation_if_needed("install", elevate):
        logger.info("Service '%s' installation delegated to elevated process", SERVICE_NAME)
        return
    kwargs: dict[str, object] = dict(
        pythonClassString=f"{__name__}.JuicerService",
        serviceName=SERVICE_NAME,
        displayName=SERVICE_DISPLAY_NAME,
        description=SERVICE_DESCRIPTION,
        startType=win32service.SERVICE_AUTO_START,
        serviceDeps=SERVICE_DEPS,
    )
    # Locate pythonservice.exe in its current site-packages/win32/ directory and
    # pass it directly. This keeps the service running under native Python and
    # avoids pywin32's built-in relocation logic, which fails on Windows Store
    # Python installs because the target directory is read-only ("Access is denied.").
    pythonservice_exe = _find_pythonservice_exe()
    if pythonservice_exe is not None:
        kwargs["exeName"] = pythonservice_exe
    win32serviceutil.InstallService(**kwargs)
    logger.info("Service '%s' installed", SERVICE_NAME)


def uninstall_service(*, elevate: bool = True) -> None:
    """Remove the Juicer Windows service."""
    _ensure_pywin32()
    if not _request_elevation_if_needed("uninstall", elevate):
        logger.info("Service '%s' removal delegated to elevated process", SERVICE_NAME)
        return
    win32serviceutil.RemoveService(SERVICE_NAME)
    logger.info("Service '%s' uninstalled", SERVICE_NAME)


def start_service(*, elevate: bool = True) -> None:
    """Start the Juicer service."""
    _ensure_pywin32()
    if not _request_elevation_if_needed("start", elevate):
        logger.info("Service '%s' start delegated to elevated process", SERVICE_NAME)
        return
    win32serviceutil.StartService(SERVICE_NAME)
    logger.info("Service '%s' started", SERVICE_NAME)


def stop_service(*, elevate: bool = True) -> None:
    """Stop the Juicer service."""
    _ensure_pywin32()
    if not _request_elevation_if_needed("stop", elevate):
        logger.info("Service '%s' stop delegated to elevated process", SERVICE_NAME)
        return
    win32serviceutil.StopService(SERVICE_NAME)
    logger.info("Service '%s' stopped", SERVICE_NAME)


def restart_service(*, elevate: bool = True) -> None:
    """Restart the Juicer service."""
    _ensure_pywin32()
    if not _request_elevation_if_needed("restart", elevate):
        logger.info("Service '%s' restart delegated to elevated process", SERVICE_NAME)
        return
    win32serviceutil.RestartService(SERVICE_NAME)
    logger.info("Service '%s' restarted", SERVICE_NAME)


def service_status() -> str:
    """Query the current status of the Juicer service.

    Returns a human-readable status string.
    """
    _ensure_pywin32()
    try:
        status = win32serviceutil.QueryServiceStatus(SERVICE_NAME)
        state_map = {
            win32service.SERVICE_STOPPED: "STOPPED",
            win32service.SERVICE_START_PENDING: "START_PENDING",
            win32service.SERVICE_STOP_PENDING: "STOP_PENDING",
            win32service.SERVICE_RUNNING: "RUNNING",
            win32service.SERVICE_CONTINUE_PENDING: "CONTINUE_PENDING",
            win32service.SERVICE_PAUSE_PENDING: "PAUSE_PENDING",
            win32service.SERVICE_PAUSED: "PAUSED",
        }
        return state_map.get(status[1], f"UNKNOWN ({status[1]})")
    except Exception as exc:
        return f"ERROR: {exc}"


def run_debug() -> None:
    """Run the service in debug/console mode (not as an SCM service)."""
    _ensure_pywin32()
    win32serviceutil.HandleCommandLine(JuicerService)


def _run_service_command_without_elevation(command: str) -> None:
    if command not in _SERVICE_COMMANDS:
        raise SystemExit(f"Unsupported service command: {command}")
    service_command = cast(ServiceCommand, command)
    if service_command == "install":
        install_service(elevate=False)
    elif service_command == "uninstall":
        uninstall_service(elevate=False)
    elif service_command == "start":
        start_service(elevate=False)
    elif service_command == "stop":
        stop_service(elevate=False)
    elif service_command == "restart":
        restart_service(elevate=False)


def main() -> None:
    """Entry point for ``python -m juicer.service``."""
    if len(sys.argv) == 3 and sys.argv[1] == "--elevated-service-command":
        _run_service_command_without_elevation(sys.argv[2])
        return

    if _PYWIN32_AVAILABLE:
        win32serviceutil.HandleCommandLine(JuicerService)
    else:
        print(
            "pywin32 is required for service operations. "
            "Install with: pip install pywin32",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
