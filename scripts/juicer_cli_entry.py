"""PyInstaller entry point for the standalone ``juicer.exe`` CLI executable.

When invoked without arguments ``juicer --help`` is shown. All Click commands
that the normal ``juicer`` script exposes are available; Windows-only service
commands fail gracefully when pywin32 is not present.

Build with::

    # from the repository root
    poetry run pyinstaller scripts/juicer_cli.spec

The output is ``dist/juicer.exe``.
"""

from __future__ import annotations

import sys

# When running directly with `python scripts/juicer_cli_entry.py` (not as a
# PyInstaller-frozen bundle) the juicer package lives in src/ relative to the
# repository root.  Add it to sys.path so the import works in both cases.
if not getattr(sys, "frozen", False):
    import os as _os

    _src = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "src")
    if _src not in sys.path:
        sys.path.insert(0, _src)

from juicer.cli import main

if __name__ == "__main__":
    main()
