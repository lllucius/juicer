"""Juicer Windows service — pywin32 ServiceFramework wrapper.

Service name: ``Juicer``
Display name: ``Juicer UPS Controller``
Auto-start, depends on Serenum + Serial.

Importable on any OS; runtime methods raise ``OSError`` on non-Windows.
"""

from __future__ import annotations

import logging
import platform
import sys

logger = logging.getLogger(__name__)

_WINDOWS = platform.system() == "Windows"

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
        import servicemanager  # type: ignore[import-not-found]
        import win32event  # type: ignore[import-not-found]
        import win32service  # type: ignore[import-not-found]
        import win32serviceutil  # type: ignore[import-not-found]

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


def _run_boot_sequence() -> None:
    """Load config from registry and run boot sequence."""
    from juicer.config import WindowsRegistryStore
    from juicer.protocol import JuicerClient, SerialTransport
    from juicer.sequence import run_boot

    store = WindowsRegistryStore()
    config = store.load()

    transport = SerialTransport(port=config.port)
    transport.open()
    try:
        client = JuicerClient(transport)
        run_boot(config, client)
    finally:
        transport.close()


def _run_shutdown_sequence() -> None:
    """Load config from registry and run shutdown sequence."""
    from juicer.config import WindowsRegistryStore
    from juicer.protocol import JuicerClient, SerialTransport
    from juicer.sequence import run_shutdown

    store = WindowsRegistryStore()
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

        def SvcDoRun(self) -> None:
            """Main service entry: boot → wait → shutdown."""
            try:
                self.ReportServiceStatus(win32service.SERVICE_START_PENDING, waitHint=5000)
                servicemanager.LogInfoMsg(f"{SERVICE_NAME}: Running boot sequence")

                try:
                    _run_boot_sequence()
                except Exception as exc:
                    servicemanager.LogErrorMsg(f"{SERVICE_NAME}: Boot failed: {exc}")
                    logger.error("Boot sequence failed: %s", exc)

                self.ReportServiceStatus(win32service.SERVICE_RUNNING)
                servicemanager.LogInfoMsg(f"{SERVICE_NAME}: Service running")

                # Block until stop event is signalled
                win32event.WaitForSingleObject(self.stop_event, win32event.INFINITE)

                # Run shutdown sequence after stop event
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
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING, waitHint=5000)
            servicemanager.LogInfoMsg(f"{SERVICE_NAME}: Stop requested")
            win32event.SetEvent(self.stop_event)

        def SvcShutdown(self) -> None:
            """Handle SERVICE_CONTROL_SHUTDOWN — run shutdown directly.

            On system shutdown there is limited time, so we run powerdown()
            directly in the handler (matching C++ behaviour).
            """
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING, waitHint=5000)
            servicemanager.LogInfoMsg(f"{SERVICE_NAME}: System shutdown — running shutdown now")
            try:
                _run_shutdown_sequence()
            except Exception as exc:
                servicemanager.LogErrorMsg(f"{SERVICE_NAME}: Shutdown failed: {exc}")
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


def _find_service_exe() -> str | None:
    """Return the path to juicer-svc.exe if it exists alongside the current executable.

    When the CLI (juicer.exe) installs the service, the Windows Service Control Manager
    must be pointed at the dedicated service host binary (juicer-svc.exe), not the CLI.
    """
    import os

    current_dir = os.path.dirname(os.path.abspath(sys.executable))
    svc_exe = os.path.join(current_dir, "juicer-svc.exe")
    return svc_exe if os.path.isfile(svc_exe) else None


def install_service() -> None:
    """Install the Juicer Windows service."""
    _ensure_pywin32()
    kwargs: dict[str, object] = dict(
        pythonClassString=f"{__name__}.JuicerService",
        serviceName=SERVICE_NAME,
        displayName=SERVICE_DISPLAY_NAME,
        description=SERVICE_DESCRIPTION,
        startType=win32service.SERVICE_AUTO_START,  # type: ignore[name-defined]
        serviceDeps=SERVICE_DEPS,
    )
    svc_exe = _find_service_exe()
    if svc_exe is not None:
        kwargs["exeName"] = svc_exe
    win32serviceutil.InstallService(**kwargs)  # type: ignore[name-defined]
    logger.info("Service '%s' installed", SERVICE_NAME)


def uninstall_service() -> None:
    """Remove the Juicer Windows service."""
    _ensure_pywin32()
    win32serviceutil.RemoveService(SERVICE_NAME)  # type: ignore[name-defined]
    logger.info("Service '%s' uninstalled", SERVICE_NAME)


def start_service() -> None:
    """Start the Juicer service."""
    _ensure_pywin32()
    win32serviceutil.StartService(SERVICE_NAME)  # type: ignore[name-defined]
    logger.info("Service '%s' started", SERVICE_NAME)


def stop_service() -> None:
    """Stop the Juicer service."""
    _ensure_pywin32()
    win32serviceutil.StopService(SERVICE_NAME)  # type: ignore[name-defined]
    logger.info("Service '%s' stopped", SERVICE_NAME)


def restart_service() -> None:
    """Restart the Juicer service."""
    _ensure_pywin32()
    win32serviceutil.RestartService(SERVICE_NAME)  # type: ignore[name-defined]
    logger.info("Service '%s' restarted", SERVICE_NAME)


def service_status() -> str:
    """Query the current status of the Juicer service.

    Returns a human-readable status string.
    """
    _ensure_pywin32()
    try:
        status = win32serviceutil.QueryServiceStatus(SERVICE_NAME)  # type: ignore[name-defined]
        state_map = {
            win32service.SERVICE_STOPPED: "STOPPED",  # type: ignore[name-defined]
            win32service.SERVICE_START_PENDING: "START_PENDING",  # type: ignore[name-defined]
            win32service.SERVICE_STOP_PENDING: "STOP_PENDING",  # type: ignore[name-defined]
            win32service.SERVICE_RUNNING: "RUNNING",  # type: ignore[name-defined]
            win32service.SERVICE_CONTINUE_PENDING: "CONTINUE_PENDING",  # type: ignore[name-defined]
            win32service.SERVICE_PAUSE_PENDING: "PAUSE_PENDING",  # type: ignore[name-defined]
            win32service.SERVICE_PAUSED: "PAUSED",  # type: ignore[name-defined]
        }
        return state_map.get(status[1], f"UNKNOWN ({status[1]})")  # type: ignore[index]
    except Exception as exc:
        return f"ERROR: {exc}"


def run_debug() -> None:
    """Run the service in debug/console mode (not as an SCM service)."""
    _ensure_pywin32()
    win32serviceutil.HandleCommandLine(JuicerService)  # type: ignore[arg-type]


if __name__ == "__main__":
    # Entry point for juicer-svc.exe.
    # Delegates to pywin32's HandleCommandLine which registers this executable
    # with the Service Control Manager and handles start/stop/install commands.
    if _PYWIN32_AVAILABLE:
        win32serviceutil.HandleCommandLine(JuicerService)  # type: ignore[arg-type]
    else:
        import sys as _sys

        print(
            "pywin32 is required for service operations. "
            "Install with: pip install pywin32",
            file=_sys.stderr,
        )
        _sys.exit(1)
