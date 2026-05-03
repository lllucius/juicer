@echo off
REM ============================================================
REM  Juicer Windows build script
REM  Produces:  dist\juicer.exe   (CLI)
REM             dist\juicer-gui.exe  (GUI)
REM
REM  Prerequisites (run once):
REM    pip install -r requirements-dev.txt -r requirements-windows.txt
REM ============================================================

setlocal enabledelayedexpansion

echo [juicer build] Building CLI executable...
pyinstaller packaging\juicer_cli.spec --noconfirm --clean
if errorlevel 1 (
    echo [juicer build] ERROR: CLI build failed.
    exit /b 1
)
echo [juicer build] CLI build succeeded: dist\juicer.exe

echo.
echo [juicer build] Building GUI executable...
pyinstaller packaging\juicer_gui.spec --noconfirm --clean
if errorlevel 1 (
    echo [juicer build] ERROR: GUI build failed.
    exit /b 1
)
echo [juicer build] GUI build succeeded: dist\juicer-gui.exe

echo.
echo [juicer build] All builds complete.
echo   CLI : dist\juicer.exe
echo   GUI : dist\juicer-gui.exe
