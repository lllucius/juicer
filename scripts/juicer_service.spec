# juicer_service.spec
#
# PyInstaller spec for building a self-contained ``juicer_service.exe``.
#
# Prerequisites
# -------------
# poetry install --with build --extras windows
#
# Build (from the repository root)
# ---------------------------------
# poetry run pyinstaller scripts/juicer_service.spec
#
# Output
# ------
# dist/juicer_service.exe   — single-file Windows service executable
#
# Deployment
# ----------
# Copy juicer_service.exe into %PROGRAMDATA%\Juicer\ or next to the Python
# interpreter running ``juicer service install``, then run:
#
#   juicer service install
#
# install_service() will detect juicer_service.exe automatically and register
# it directly as the service binary.

import os
import sys
from pathlib import Path

# The entry-point script lives alongside this spec file.
_here = Path(SPECPATH)
_entry = str(_here / "juicer_service_entry.py")

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
        # pywin32 service infrastructure
        "win32timezone",
        "win32api",
        "win32con",
        "win32service",
        "win32serviceutil",
        "win32event",
        "pywintypes",
        "servicemanager",
        # juicer package and its runtime deps
        "juicer",
        "juicer.service",
        "juicer.config",
        "juicer.protocol",
        "juicer.sequence",
        # pydantic (used by juicer.config)
        "pydantic",
        "pydantic.v1",
        "pydantic_core",
        # pyserial (used by juicer.protocol)
        "serial",
        "serial.serialutil",
        "serial.serialwin32",
        "serial.win32",
        # winreg / winsound (Windows stdlib, may be auto-found but list explicitly)
        "winreg",
        "winsound",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # GUI toolkit is not needed in the service
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
    name="juicer_service",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # Keep UPX off; some AV software rejects UPX-packed service exes
    upx_exclude=[],
    runtime_tmpdir=None,
    # console=True keeps early crash output visible to Windows Error Reporting.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
