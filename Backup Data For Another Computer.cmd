@echo off
title Backup Price Pulse Data
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Part Pulse is not set up on this computer yet.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" move_to_another_computer.py backup
echo.
pause
