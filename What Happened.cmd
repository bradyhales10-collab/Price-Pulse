@echo off
title What Happened In The Last Price Check
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Part Pulse is not set up yet on this computer.
  echo.
  pause
  exit /b 1
)

rem Written to a file as well as the screen, so the output survives even if the
rem window closes, and can be sent on for someone else to read.
".venv\Scripts\python.exe" explain_last_run.py > what_happened.txt 2>&1
type what_happened.txt

echo.
echo A copy was saved to what_happened.txt in this folder.
echo.
pause
