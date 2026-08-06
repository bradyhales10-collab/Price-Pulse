@echo off
title Recover Lost Price Check Results
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Part Pulse is not set up yet on this computer.
  echo.
  pause
  exit /b 1
)

echo This finds price check results that were collected in the last 72 hours
echo but never made it into the database, and imports them directly.
echo Nothing is deleted or changed except adding these results in.
echo.
".venv\Scripts\python.exe" recover_lost_results.py

echo.
pause
