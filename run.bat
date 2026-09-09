@echo off
rem ============================================================
rem  Face Recognition App - Quick launcher (double-click to run)
rem  ASCII-only file: cmd.exe cannot parse UTF-8 characters.
rem ============================================================
chcp 65001 >nul
cd /d "%~dp0"

rem Fix Qt plugin path (project path contains spaces)
set "QT_PLUGIN_PATH=%~dp0.venv\Lib\site-packages\PySide6\plugins"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Cannot find .venv\Scripts\python.exe
    echo Please check that the venv folder exists.
    pause
    exit /b 1
)

echo Starting Face Recognition App...
.venv\Scripts\python.exe -m app.main
echo.
echo App closed. Press any key to exit.
pause
