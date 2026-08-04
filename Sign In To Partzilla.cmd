@echo off
title Sign In To Partzilla
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Part Pulse is not set up yet on this computer.
  echo Double-click "Start Part Pulse.cmd" once first.
  echo.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" sign_in.py --competitor partzilla

echo.
pause
