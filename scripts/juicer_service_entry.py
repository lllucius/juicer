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

    pyinstaller scripts/juicer_service.spec

from the repository root.  The output is ``dist/juicer_service.exe``.
Drop that file next to the Python interpreter (or in ``%PROGRAMDATA%\\Juicer``)
before running ``juicer service install`` so that ``install_service()`` picks
it up automatically.
"""

from __future__ import annotations

import ctypes
import sys


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
        import servicemanager

        from juicer.service import JuicerService

        _early_evtlog("juicer_service.exe: imports OK, calling StartServiceCtrlDispatcher")
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(JuicerService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        # Manual invocation: install, uninstall, start, stop, debug, …
        _early_evtlog(f"juicer_service.exe: HandleCommandLine args={sys.argv[1:]!r}")
        import win32serviceutil

        from juicer.service import JuicerService

        win32serviceutil.HandleCommandLine(JuicerService)


if __name__ == "__main__":
    main()
