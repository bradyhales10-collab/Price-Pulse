@echo off
title Compare Pricing Engines
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Part Pulse is not set up on this computer yet.
  pause
  exit /b 1
)
echo Comparing the current pricing engine with the new one.
echo Nothing is changed - no prices are written.
echo.
".venv\Scripts\python.exe" compare_pricing_engines.py --show disagreements > pricing_comparison.txt 2>&1
type pricing_comparison.txt
echo.
echo A copy was saved to pricing_comparison.txt in this folder.
echo.
pause
