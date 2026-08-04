@echo off
title What Happened In The Last Price Check
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Part Pulse is not set up yet on this computer.
  echo.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" explain_last_run.py

echo.
pause
