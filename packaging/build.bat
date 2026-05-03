@echo off
REM ============================================================
REM  Juicer Windows build script
REM  Produces:  dist\juicer.exe      (CLI)
REM             dist\juicer-gui.exe  (GUI)
REM             dist\juicer-svc.exe  (Windows service)
REM
REM  Prerequisites (run once):
REM    pip install -r requirements-dev.txt -r requirements-windows.txt
REM    pip install -e .
REM ============================================================

setlocal enabledelayedexpansion

set PYTHONPATH=src

echo [juicer build] Building CLI executable...
python -m nuitka ^
    --onefile ^
    --output-dir=dist ^
    --output-filename=juicer.exe ^
    --include-package=juicer ^
    --nofollow-import-to=PySide6 ^
    --nofollow-import-to=tkinter ^
    --assume-yes-for-downloads ^
    src\juicer\cli.py
if errorlevel 1 (
    echo [juicer build] ERROR: CLI build failed.
    exit /b 1
)
echo [juicer build] CLI build succeeded: dist\juicer.exe

echo.
echo [juicer build] Building GUI executable...
python -m nuitka ^
    --onefile ^
    --output-dir=dist ^
    --output-filename=juicer-gui.exe ^
    --enable-plugin=pyside6 ^
    --include-package=juicer ^
    --nofollow-import-to=tkinter ^
    --windows-console-mode=disable ^
    --assume-yes-for-downloads ^
    src\juicer\gui.py
if errorlevel 1 (
    echo [juicer build] ERROR: GUI build failed.
    exit /b 1
)
echo [juicer build] GUI build succeeded: dist\juicer-gui.exe

echo.
echo [juicer build] Building service executable...
python -m nuitka ^
    --onefile ^
    --output-dir=dist ^
    --output-filename=juicer-svc.exe ^
    --include-package=juicer ^
    --nofollow-import-to=PySide6 ^
    --nofollow-import-to=tkinter ^
    --windows-console-mode=disable ^
    --assume-yes-for-downloads ^
    src\juicer\service.py
if errorlevel 1 (
    echo [juicer build] ERROR: Service build failed.
    exit /b 1
)
echo [juicer build] Service build succeeded: dist\juicer-svc.exe

echo.
echo [juicer build] All builds complete.
echo   CLI     : dist\juicer.exe
echo   GUI     : dist\juicer-gui.exe
echo   Service : dist\juicer-svc.exe
