"""PyInstaller entry point for the standalone ``juicer_service.exe``.

When the Windows Service Control Manager starts this executable it passes no
command-line arguments.  We detect that case and call
``servicemanager.StartServiceCtrlDispatcher``, which registers with the SCM
and drives the full ``JuicerService`` lifecycle without any involvement from
``pythonservice.exe``.

When the user runs the exe manually with arguments (``install``, ``uninstall``,
``start``, ``stop``, ``debug``, etc.) we delegate to
``win32serviceutil.HandleCommandLine`` as usual.

Build with::

    # from the repository root
    PyInstaller scripts/juicer_service.spec

The output is ``dist/juicer_service.exe``.
Drop that file next to the Python interpreter (or in ``%PROGRAMDATA%\\Juicer``)
before running ``juicer service install`` so that ``install_service()`` picks
it up automatically.
"""

from __future__ import annotations

import ctypes
import sys

# When running directly with `python scripts/juicer_service_entry.py` (not as a
# PyInstaller-frozen bundle) the juicer package lives in src/ relative to the
# repository root.  Add it to sys.path so the import works in both cases.
if not getattr(sys, "frozen", False):
    import os as _os

    _src = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "src")
    if _src not in sys.path:
        sys.path.insert(0, _src)


# ── Earliest possible diagnostic breadcrumb ──────────────────────────────────
# This fires before any juicer or pywin32 imports.  If it appears in Event
# Viewer the executable itself is loading correctly; if it does NOT appear the
# crash happens at the OS loader / C-runtime level before Python runs at all.
def _early_evtlog(message: str, *, error: bool = False) -> None:
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return
    try:
        advapi32 = windll.advapi32
        event_type = ctypes.c_ushort(1 if error else 4)
        h = advapi32.RegisterEventSourceW(None, "Juicer")
        if h:
            c_strings = (ctypes.c_wchar_p * 1)(message)
            advapi32.ReportEventW(
                h,
                event_type,
                ctypes.c_ushort(0),
                ctypes.c_uint(0),
                None,
                ctypes.c_ushort(1),
                ctypes.c_uint(0),
                c_strings,
                None,
            )
            advapi32.DeregisterEventSource(h)
    except Exception:
        pass


_early_evtlog(
    f"juicer_service.exe: entry point reached — "
    f"exe={sys.executable!r} args={sys.argv[1:]!r}"
)


def main() -> None:
    """Dispatch to SCM dispatcher or management command handler."""
    # Running as a Windows service (SCM provides no arguments).
    if len(sys.argv) == 1:
        _early_evtlog("juicer_service.exe: starting SCM dispatcher")
        try:
            import servicemanager
        except ImportError as exc:
            msg = (
                f"juicer_service: cannot import servicemanager — {exc}\n"
                "Make sure pywin32 is installed: pip install pywin32"
            )
            _early_evtlog(msg, error=True)
            print(msg, file=sys.stderr)
            sys.exit(1)

        try:
            from juicer.service import JuicerService
        except Exception as exc:
            import traceback

            msg = f"juicer_service: cannot import juicer.service — {exc}\n{traceback.format_exc()}"
            _early_evtlog(msg, error=True)
            print(msg, file=sys.stderr)
            sys.exit(1)

        _early_evtlog("juicer_service.exe: imports OK, calling StartServiceCtrlDispatcher")
        try:
            servicemanager.Initialize()
            servicemanager.PrepareToHostSingle(JuicerService)
            servicemanager.StartServiceCtrlDispatcher()
        except Exception as exc:
            import traceback

            msg = (
                f"juicer_service: StartServiceCtrlDispatcher failed — {exc}\n"
                f"{traceback.format_exc()}"
            )
            _early_evtlog(msg, error=True)
            print(msg, file=sys.stderr)
            sys.exit(1)
    else:
        # Manual invocation: install, uninstall, start, stop, debug, …
        _early_evtlog(f"juicer_service.exe: HandleCommandLine args={sys.argv[1:]!r}")
        try:
            import win32serviceutil

            from juicer.service import JuicerService
        except ImportError as exc:
            msg = (
                f"juicer_service: cannot import win32serviceutil or juicer.service — {exc}\n"
                "Make sure pywin32 is installed: pip install pywin32"
            )
            _early_evtlog(msg, error=True)
            print(msg, file=sys.stderr)
            sys.exit(1)

        win32serviceutil.HandleCommandLine(JuicerService)


if __name__ == "__main__":
    main()
