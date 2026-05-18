# juicer_gui.spec
#
# PyInstaller spec for building a self-contained ``juicer-gui.exe`` executable.
#
# Prerequisites
# -------------
# poetry install --with build --extras gui,windows
#
# Build (from the repository root)
# ---------------------------------
# poetry run pyinstaller scripts/juicer_gui.spec
#
# Output
# ------
# dist/juicer-gui.exe   — single-file Windows GUI executable

import sys
from pathlib import Path

# The entry-point script lives alongside this spec file.
_here = Path(SPECPATH)
_entry = str(_here / "juicer_gui_entry.py")

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
        "juicer.gui",
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
        # click (used by juicer.cli, imported by juicer.gui for service tab)
        "click",
        # winreg / winsound (Windows stdlib)
        "winreg",
        "winsound",
        # pywin32 (optional; service tab)
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
    excludes=[],
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
    name="juicer-gui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    # windowed=True hides the console window for a GUI application.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
