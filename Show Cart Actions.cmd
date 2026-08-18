@echo off
title Show What The Cart Cleanup Did
cd /d "%~dp0"
if not exist "data\output\cart_actions.log" (
  echo No cart actions have been recorded yet.
  echo Run a MotoSport price check first.
  echo.
  pause
  exit /b 0
)
echo The last 200 cart actions:
echo.
powershell -NoProfile -Command "Get-Content 'data\output\cart_actions.log' -Tail 200"
echo.
echo The full record is in data\output\cart_actions.log
echo Send that file to Claude.
echo.
pause
