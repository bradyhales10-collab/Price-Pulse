@echo off
title Stop Part Pulse
cd /d "%~dp0"
set "PP_ROOT=%CD%"

echo ===============================
echo   Stopping Part Pulse
echo ===============================
echo.

taskkill /F /FI "WINDOWTITLE eq Part Pulse Browser Helper*" >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq Part Pulse Collector*" >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq Part Pulse Dashboard*" >nul 2>&1

echo Part Pulse has been stopped.
echo.
echo The website at http://127.0.0.1:8000 will no longer load
echo until you double-click "Start Part Pulse.cmd" again.
echo.
pause
