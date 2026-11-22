@echo off
REM ==========================================================
REM  Payeshgar - stop running instances
REM ==========================================================
chcp 65001 >nul 2>nul
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$here = (Get-Location).Path; Get-CimInstance Win32_Process | Where-Object { ($_.CommandLine -match 'run\.py' -or $_.CommandLine -match 'launcher\.py' -or $_.CommandLine -match 'app\.py') -and $_.CommandLine.IndexOf($here, [System.StringComparison]::OrdinalIgnoreCase) -ge 0 } | ForEach-Object { if ($_.ProcessId -ne $PID) { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } }"
echo Stopped running Payeshgar instances.
pause
endlocal
