"""Juicer Windows service — pywin32 ServiceFramework wrapper.

Service name: ``Juicer``
Display name: ``Juicer UPS Controller``
Auto-start, depends on Serenum + Serial.

Importable on any OS; runtime methods raise ``OSError`` on non-Windows.
"""

from __future__ import annotations

import ctypes
import logging
import ntpath
import os
import platform
import site
import subprocess
import sys
import tempfile
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Literal, cast, get_args

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


def _evtlog(message: str, *, error: bool = False) -> None:
    """Write directly to the Windows Application Event Log using pure ctypes.

    This helper has **no** pywin32 dependency: it calls ``advapi32.dll``
    directly via :mod:`ctypes`, which is always available in the standard
    library.  It can therefore fire at any point in the service process
    lifetime, including module-level code that runs before pywin32 is
    imported.

    Events are written to the ``Application`` log under source ``Juicer``.
    *All* errors are silently swallowed — this is diagnostic-only and must
    never crash the process that calls it.
    """
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return
    try:
        advapi32 = windll.advapi32
        # EVENTLOG_ERROR_TYPE = 1  EVENTLOG_INFORMATION_TYPE = 4
        event_type = ctypes.c_ushort(1 if error else 4)
        h = advapi32.RegisterEventSourceW(None, SERVICE_NAME)
        if h:
            c_strings = (ctypes.c_wchar_p * 1)(message)
            advapi32.ReportEventW(
                h,
                event_type,
                ctypes.c_ushort(0),  # wCategory
                ctypes.c_uint(0),  # dwEventID (0 = generic)
                None,  # lpUserSid
                ctypes.c_ushort(1),  # wNumStrings
                ctypes.c_uint(0),  # dwDataSize
                c_strings,
                None,  # lpRawData
            )
            advapi32.DeregisterEventSource(h)
    except Exception:  # pragma: no cover
        pass


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

# ── Module-level startup breadcrumb ───────────────────────────────────
# When pythonservice.exe loads this module as a service class, emit a
# checkpoint to the Windows Application Event Log *before* any class
# methods run.  This is the earliest possible diagnostic point: if this
# message appears in Event Viewer but SvcDoRun never fires, the failure
# is in class instantiation; if this message does NOT appear, the
# failure is even earlier (Python interpreter start, wrong executable,
# or a crash before this line).
_IS_PYTHONSERVICE = _WINDOWS and "pythonservice" in Path(sys.executable).name.lower()

if _IS_PYTHONSERVICE:
    _evtlog(
        f"{SERVICE_NAME}: service.py module loaded — "
        f"exe={sys.executable!r} "
        f"prefix={sys.prefix!r} "
        f"pywin32={_PYWIN32_AVAILABLE} "
        f"path={sys.path[:4]!r}"
    )


def _ensure_pywin32() -> None:
    """Raise a descriptive error when pywin32-backed service features are unavailable."""
    if not _PYWIN32_AVAILABLE:
        raise OSError(
            "pywin32 is required for service operations. "
            "Install with: pip install pywin32"
        )


ProgressCallback = Callable[[], None]
ServiceCommand = Literal["install", "uninstall", "start", "stop", "restart"]
ServiceCommandHandler = Callable[..., None]
_SERVICE_COMMANDS: frozenset[str] = frozenset(get_args(ServiceCommand))


def _windows_dll(name: str) -> Any | None:
    """Return a Windows DLL object from ctypes.windll, or None when unavailable."""
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
    """Run a service management command through Windows UAC and wait for it.

    The elevated subprocess writes any errors to a temporary log file whose
    contents are included in the raised ``OSError`` so the GUI surfaces a real
    diagnostic instead of just "exit code 1".
    """
    if command not in _SERVICE_COMMANDS:
        raise ValueError(f"Unsupported elevated service command: {command}")

    shell32 = _windows_dll("shell32")
    kernel32 = _windows_dll("kernel32")
    if shell32 is None or kernel32 is None:
        raise OSError("Unable to request elevated privileges on this platform")

    log_fd, log_path_str = tempfile.mkstemp(prefix="juicer-elevated-", suffix=".log")
    os.close(log_fd)
    try:
        params = subprocess.list2cmdline(
            [
                "-m",
                "juicer.service",
                "--elevated-service-command",
                command,
                "--elevated-log-path",
                log_path_str,
            ]
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
        sei.lpDirectory = _service_package_parent()
        sei.nShow = SW_SHOWNORMAL

        if not shell_execute_ex(ctypes.byref(sei)):
            raise OSError("Windows elevation request was cancelled or failed")

        try:
            kernel32.WaitForSingleObject(sei.hProcess, INFINITE)
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(sei.hProcess, ctypes.byref(exit_code)):
                raise OSError("Unable to read elevated process exit code")
            if exit_code.value != 0:
                raise OSError(_format_elevated_failure(command, exit_code.value, log_path_str))
        finally:
            kernel32.CloseHandle(sei.hProcess)
    finally:
        try:
            Path(log_path_str).unlink()
        except OSError:
            pass


def _format_elevated_failure(command: str, exit_code: int, log_path: str) -> str:
    """Build an OSError message that includes the captured subprocess log."""
    details = ""
    try:
        details = Path(log_path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        details = ""
    header = f"Elevated service {command} failed with exit code {exit_code}"
    if details:
        return f"{header}.\nDetails:\n{details}"
    return (
        f"{header}; run the command from an elevated console for details "
        "(no output was captured from the elevated process)"
    )


def _request_elevation_if_needed(command: ServiceCommand, elevate: bool) -> bool:
    """Request UAC elevation when needed; return whether the caller should continue."""
    if _WINDOWS and elevate and not _is_user_admin():
        _request_elevated_service_command(command)
        return False
    return True


def _default_service_log_dir() -> Path:
    """Return the default service log directory without importing juicer.config.

    This mirrors the platform default used by the TOML config store but does
    not import :mod:`juicer.config`, so it remains usable when configuration
    loading itself fails.
    """
    if _WINDOWS:
        base = os.environ.get("PROGRAMDATA")
        if base:
            return Path(base) / "Juicer"
    return Path.home() / ".juicer"


def _service_log_path(name: str) -> Path:
    """Return a service log path next to the TOML configuration.

    Falls back to :func:`_default_service_log_dir` if importing
    :mod:`juicer.config` (or constructing :class:`TomlStore`) fails.  This
    guarantees that early-startup logging works even when configuration
    handling is broken — exactly the scenario that produced "no log files"
    reports from the Windows service.
    """
    try:
        from juicer.config import TomlStore

        config_path = TomlStore().path
        return config_path.with_name(f"{name}.log")
    except Exception:
        return _default_service_log_dir() / f"{name}.log"


def _install_service_log_handler(name: str) -> tuple[logging.Handler, Path] | None:
    """Add a root-logger file handler for the service and return it.

    Returns ``None`` if the log file cannot be created.  Callers are
    responsible for removing the handler when the scope ends.
    """
    try:
        path = _service_log_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8")
    except OSError:
        return None
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    if root_logger.level == logging.NOTSET or root_logger.level > logging.INFO:
        root_logger.setLevel(logging.INFO)
    return handler, path


def _remove_service_log_handler(handler: logging.Handler) -> None:
    """Remove a previously installed service log handler from the root logger."""
    logging.getLogger().removeHandler(handler)
    handler.close()


def _service_package_parent() -> str:
    """Return the directory that must be importable to load ``juicer.service``."""
    # service.py lives in the juicer package; the package parent is the import root.
    return str(Path(__file__).resolve().parents[1])


def _service_python_class_string() -> str:
    """Return a pywin32 class string that also works from source checkouts.

    ``pythonservice.exe`` supports a ``path\\module.Class`` class string and
    prepends that path to ``sys.path`` before importing the service class.  This
    keeps services installed from an unpacked source tree importable when the
    package has not been installed into site-packages.
    """
    return ntpath.join(_service_package_parent(), f"{__name__}.JuicerService")


@contextmanager
def _service_file_logging(name: str) -> Iterator[Path]:
    """Temporarily route service Python logging to a sequence-specific file."""
    root_logger = logging.getLogger()
    previous_level = root_logger.level
    installed = _install_service_log_handler(name)
    if installed is None:
        # Logging setup failed (e.g. read-only filesystem in tests).  Yield a
        # best-effort path so the contract is preserved.
        path = _default_service_log_dir() / f"{name}.log"
        yield path
        return
    handler, path = installed
    try:
        logger.info("Writing %s service log to %s", name, path)
        yield path
    finally:
        logger.info("Finished writing %s service log to %s", name, path)
        _remove_service_log_handler(handler)
        root_logger.setLevel(previous_level)


class _Win32CancelToken:
    """Cancellation token backed by a Win32 event handle."""

    def __init__(self, event: object) -> None:
        """Wrap the raw Win32 event handle used by the service lifecycle."""
        self._event = event

    def is_set(self) -> bool:
        """Return whether the wrapped stop event has been signalled."""
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
            """Create the service instance and allocate its stop event handle."""
            _evtlog(f"{SERVICE_NAME}: JuicerService.__init__ reached")
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

            A root-logger file handler is installed at the very top of this
            method so that *any* failure during service startup — including
            failures that happen before the boot sequence runs — is recorded
            in ``service.log`` next to the configuration file.  Without this
            handler, early failures left ``C:\\ProgramData\\Juicer`` empty and
            users had no way to diagnose service start timeouts.
            """
            _evtlog(f"{SERVICE_NAME}: SvcDoRun reached")
            installed = _install_service_log_handler("service")
            service_log_handler = installed[0] if installed is not None else None
            service_log_path = installed[1] if installed is not None else None
            try:
                if service_log_path is not None:
                    logger.info("Juicer service starting; log file: %s", service_log_path)
                else:
                    servicemanager.LogWarningMsg(
                        f"{SERVICE_NAME}: Unable to open service.log for writing"
                    )
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
                        logger.exception("Boot sequence failed")
                        return

                    self._reporting_start_pending = False
                    self.ReportServiceStatus(win32service.SERVICE_RUNNING)
                    servicemanager.LogInfoMsg(f"{SERVICE_NAME}: Service running")

                    # Block until stop event is signalled
                    win32event.WaitForSingleObject(self.stop_event, win32event.INFINITE)

                    # Run shutdown sequence after stop event unless SvcShutdown already did it.
                    if not self._shutdown_done:
                        servicemanager.LogInfoMsg(
                            f"{SERVICE_NAME}: Running shutdown sequence"
                        )
                        try:
                            _run_shutdown_sequence()
                        except Exception as exc:
                            servicemanager.LogErrorMsg(
                                f"{SERVICE_NAME}: Shutdown failed: {exc}"
                            )
                            logger.exception("Shutdown sequence failed")

                except Exception:
                    # Catch-all so that unexpected failures (e.g. transient
                    # import errors, SCM API failures) still produce a record
                    # in service.log and the Windows event log.
                    logger.exception("Unhandled exception in SvcDoRun")
                    try:
                        servicemanager.LogErrorMsg(
                            f"{SERVICE_NAME}: Unhandled exception: {traceback.format_exc()}"
                        )
                    except Exception:
                        pass
                    raise

                finally:
                    self.ReportServiceStatus(win32service.SERVICE_STOPPED)
                    servicemanager.LogInfoMsg(f"{SERVICE_NAME}: Service stopped")
            finally:
                if service_log_handler is not None:
                    _remove_service_log_handler(service_log_handler)

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
            """Fail fast on non-Windows platforms where the real service cannot run."""
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


def _running_in_venv() -> bool:
    """Return True when the current interpreter is running inside a virtual env."""
    base_prefix = getattr(sys, "base_prefix", sys.prefix)
    return os.path.normcase(sys.prefix) != os.path.normcase(base_prefix)


def _compute_service_environment() -> list[str]:
    """Build the ``Environment`` REG_MULTI_SZ entries for the installed service.

    When Juicer is installed from a virtual environment, ``pythonservice.exe``
    is launched by the SCM **without** venv activation: the venv's
    ``site-packages`` directory is not on ``sys.path``, so importing
    :mod:`juicer.service` (which transitively requires ``pydantic`` and other
    third-party dependencies) fails before :meth:`SvcDoRun` ever runs.  The
    symptom is SCM error 1053 with no entry in ``service.log``, because the
    service process exits during module import.

    Writing the current installer's ``sys.path`` as ``PYTHONPATH`` into the
    service's environment registry value makes the service process inherit
    the same import paths as the installer, which is exactly what is needed
    for venv-based installs.  ``PYTHONHOME`` is also exported when running
    from a venv so that Python's start-up resolves the venv's standard
    library and ``site-packages`` correctly.

    Only existing directory entries are included.  Empty / non-existent
    entries (e.g. the current directory, zipapp paths, or missing site
    directories) are filtered out so they do not pollute the service's
    import path.
    """
    seen: set[str] = set()
    paths: list[str] = []
    for entry in sys.path:
        if not entry:
            continue
        try:
            if not os.path.isdir(entry):
                continue
        except OSError:
            continue
        normalized = os.path.normcase(os.path.normpath(entry))
        if normalized in seen:
            continue
        seen.add(normalized)
        paths.append(entry)

    env: list[str] = []
    if paths:
        env.append("PYTHONPATH=" + os.pathsep.join(paths))
    if _running_in_venv():
        env.append(f"PYTHONHOME={sys.prefix}")
    return env


def _write_service_environment(env_entries: list[str]) -> None:
    """Write ``Environment`` REG_MULTI_SZ for the Juicer service.

    The SCM merges this value into the service process's environment block at
    start time, so any variable set here is visible to ``pythonservice.exe``
    and the embedded Python interpreter it hosts.  This function is a no-op
    when ``env_entries`` is empty or when the registry is unavailable (for
    example on non-Windows systems used in tests).
    """
    if not env_entries:
        return
    try:
        import winreg
    except ImportError:
        logger.debug("winreg unavailable; skipping service environment write")
        return

    key_path = rf"SYSTEM\CurrentControlSet\Services\{SERVICE_NAME}"
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            key_path,
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(key, "Environment", 0, winreg.REG_MULTI_SZ, env_entries)
    except OSError as exc:
        # Don't fail the install just because we couldn't write the env block —
        # the service may still work when installed against a system Python.
        logger.warning(
            "Unable to write service environment to registry "
            "(HKLM\\%s\\Environment): %s",
            key_path,
            exc,
        )
        return
    logger.info(
        "Wrote %d service environment variable(s) to HKLM\\%s\\Environment",
        len(env_entries),
        key_path,
    )


def install_service(*, elevate: bool = True) -> None:
    """Install the Juicer Windows service."""
    _ensure_pywin32()
    if not _request_elevation_if_needed("install", elevate):
        logger.info("Service '%s' installation delegated to elevated process", SERVICE_NAME)
        return
    kwargs: dict[str, object] = dict(
        pythonClassString=_service_python_class_string(),
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
    # Propagate the installer's sys.path (and PYTHONHOME for venv installs)
    # into the service's environment so the embedded Python interpreter can
    # import juicer and its third-party dependencies (pydantic, etc.) when
    # the SCM launches pythonservice.exe.  Without this, a venv-based install
    # always hits SCM 1053 because module import fails before SvcDoRun runs.
    _write_service_environment(_compute_service_environment())
    logger.info("Service '%s' installed", SERVICE_NAME)
    logger.info(
        "If start fails with SCM 1053, check the Windows Application event log "
        "for entries from source 'PythonService' for Python import errors that "
        "happened before %s was created.",
        _service_log_path("service"),
    )


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
    try:
        win32serviceutil.StartService(SERVICE_NAME)
    except Exception as exc:
        log_path = _service_log_path("service")
        boot_log_path = _service_log_path("boot")
        raise OSError(
            f"Failed to start service '{SERVICE_NAME}': {exc}\n"
            f"Check the service log at {log_path} and the boot log at "
            f"{boot_log_path}, and consult the Windows Event Viewer "
            "(Windows Logs → Application) for entries from source "
            f"'{SERVICE_NAME}'."
        ) from exc
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
    """Dispatch a validated service command without triggering another UAC prompt."""
    if command not in _SERVICE_COMMANDS:
        raise SystemExit(f"Unsupported service command: {command}")
    service_command = cast(ServiceCommand, command)
    handlers: dict[ServiceCommand, ServiceCommandHandler] = {
        "install": install_service,
        "uninstall": uninstall_service,
        "start": start_service,
        "stop": stop_service,
        "restart": restart_service,
    }
    handlers[service_command](elevate=False)


def _run_elevated_command_with_logging(command: str, log_path: str | None) -> int:
    """Run a service command in the elevated subprocess and capture failures.

    Returns the desired process exit code.  When ``log_path`` is provided, any
    captured exception traceback or printed output is written to that file so
    the parent process can display a meaningful error message instead of just
    "exit code 1".
    """
    buffer: list[str] = []

    def emit(line: str) -> None:
        buffer.append(line)
        try:
            print(line, file=sys.stderr)
        except OSError:
            pass

    try:
        _run_service_command_without_elevation(command)
        return 0
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        if exc.code is not None and not isinstance(exc.code, int):
            emit(str(exc.code))
        return code
    except BaseException:
        emit(f"Elevated service {command} raised an exception:")
        emit(traceback.format_exc().rstrip())
        return 1
    finally:
        if log_path and buffer:
            try:
                Path(log_path).write_text("\n".join(buffer) + "\n", encoding="utf-8")
            except OSError:
                pass


def main() -> None:
    """Entry point for ``python -m juicer.service``."""
    argv = sys.argv[1:]
    log_path: str | None = None
    if "--elevated-log-path" in argv:
        idx = argv.index("--elevated-log-path")
        if idx + 1 >= len(argv):
            print("--elevated-log-path requires a value", file=sys.stderr)
            sys.exit(2)
        log_path = argv[idx + 1]
        del argv[idx : idx + 2]

    if len(argv) == 2 and argv[0] == "--elevated-service-command":
        sys.exit(_run_elevated_command_with_logging(argv[1], log_path))

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
