# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for the Juicer CLI executable.
#
# Build with:
#   pyinstaller packaging/juicer_cli.spec
#
# Output: dist/juicer.exe  (Windows)  /  dist/juicer  (Linux/macOS)

block_cipher = None

a = Analysis(
    ["../src/juicer/cli.py"],
    pathex=["../src"],
    binaries=[],
    datas=[],
    hiddenimports=[
        "juicer",
        "juicer.cli",
        "juicer.config",
        "juicer.protocol",
        "juicer.sequence",
        "juicer.service",
        "serial",
        "serial.tools",
        "serial.tools.list_ports",
        "pydantic",
        "click",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PySide6", "tkinter"],
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
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,      # CLI: keep the console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
