@echo off
title Check Part Pulse
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Part Pulse is not set up yet on this computer.
  echo Double-click "Start Part Pulse.cmd" once first.
  echo.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" diagnose_part_pulse.py

echo.
pause
