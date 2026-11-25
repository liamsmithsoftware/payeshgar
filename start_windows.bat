@echo off
REM ==========================================================
REM  Payeshgar - Windows launcher
REM ==========================================================
chcp 65001 >nul 2>nul
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" launcher.py %*
    exit /b %errorlevel%
)

py -3 launcher.py %* 2>nul
if not errorlevel 1 exit /b 0
python launcher.py %*
pause
endlocal
