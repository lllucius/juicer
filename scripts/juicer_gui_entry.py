"""PyInstaller entry point for the standalone ``juicer-gui.exe`` executable.

Launches the PySide6 desktop GUI. Exits with a helpful error message when
PySide6 is not bundled into the executable.

Build with::

    # from the repository root
    poetry run pyinstaller scripts/juicer_gui.spec

The output is ``dist/juicer-gui.exe``.
"""

from __future__ import annotations

import sys

# When running directly with `python scripts/juicer_gui_entry.py` (not as a
# PyInstaller-frozen bundle) the juicer package lives in src/ relative to the
# repository root.  Add it to sys.path so the import works in both cases.
if not getattr(sys, "frozen", False):
    import os as _os

    _src = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "src")
    if _src not in sys.path:
        sys.path.insert(0, _src)

from juicer.gui import main

if __name__ == "__main__":
    main()
