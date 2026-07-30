@echo off
title Stop Part Pulse
cd /d "%~dp0"
set "PP_ROOT=%CD%"

echo ===============================
echo   Stopping Part Pulse
echo ===============================
echo.

powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'python' -and $_.CommandLine -like ('*' + $env:PP_ROOT + '*') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1

echo Part Pulse has been stopped.
echo.
echo The website at http://127.0.0.1:8000 will no longer load
echo until you double-click "Start Part Pulse.cmd" again.
echo.
pause
