# juicer_cli.spec
#
# PyInstaller spec for building a self-contained ``juicer.exe`` CLI executable.
#
# Prerequisites
# -------------
# poetry install --with build --extras windows
#
# Build (from the repository root)
# ---------------------------------
# poetry run pyinstaller scripts/juicer_cli.spec
#
# Output
# ------
# dist/juicer.exe   — single-file Windows CLI executable

import sys
from pathlib import Path

# The entry-point script lives alongside this spec file.
_here = Path(SPECPATH)
_entry = str(_here / "juicer_cli_entry.py")

# Add the juicer source tree so Analysis can resolve the package.
_src = str(_here.parent / "src")
if _src not in sys.path:
    sys.path.insert(0, _src)

block_cipher = None

a = Analysis(
    [_entry],
    pathex=[_src],
    binaries=[],
    datas=[],
    hiddenimports=[
        # juicer package and its runtime deps
        "juicer",
        "juicer.cli",
        "juicer.config",
        "juicer.protocol",
        "juicer.sequence",
        "juicer.service",
        # pydantic (used by juicer.config)
        "pydantic",
        "pydantic.v1",
        "pydantic_core",
        # pyserial (used by juicer.protocol)
        "serial",
        "serial.serialutil",
        "serial.serialwin32",
        "serial.win32",
        # click
        "click",
        # winreg (Windows stdlib; used by juicer.config / juicer.service)
        "winreg",
        # pywin32 (optional; service commands only)
        "win32api",
        "win32con",
        "win32service",
        "win32serviceutil",
        "win32event",
        "pywintypes",
        "servicemanager",
        "win32timezone",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # GUI toolkit is not needed in the CLI
        "PySide6",
        "PyQt5",
        "PyQt6",
        "tkinter",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="juicer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
