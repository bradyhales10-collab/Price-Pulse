@echo off
title RevZilla Probe
cd /d "%~dp0"

echo ===============================
echo   RevZilla Price Check Test
echo ===============================
echo.
echo This is a TEST, not a real price check.
echo.
echo It will open a browser and look up a small number of Kawasaki parts
echo on RevZilla, waiting 6 seconds between each one. Nothing is saved to
echo your database and no prices are changed.
echo.
echo A browser window will open and move on its own. That is expected.
echo Please do not click inside it while it runs.
echo.
pause
echo.

if not exist ".venv\Scripts\python.exe" (
  echo ERROR: Part Pulse is not set up yet on this computer.
  echo Double-click "Start Part Pulse.cmd" once first.
  echo.
  pause
  exit /b 1
)

echo Getting the latest version first, so the test uses current code...
where git >nul 2>&1
if errorlevel 1 (
  echo   WARNING: Git is not installed, so this may be testing an old version.
) else (
  git pull origin main
  if errorlevel 1 (
    echo.
    echo   WARNING: Could not update. This may be testing an OLD version,
    echo   which will give misleading results. Consider running
    echo   "Repair Part Pulse.cmd" before trusting the output.
    echo.
    pause
  ) else (
    echo   Up to date.
  )
)
echo.

for /f "delims=" %%v in ('git rev-parse --short HEAD 2^>nul') do set "PP_VER=%%v"
echo Testing code version: %PP_VER%
echo.

".venv\Scripts\python.exe" probe_competitor.py --competitor revzilla --file "data\input\RevZilla_Probe_Parts.csv" --max-parts 7 --delay-seconds 6

echo.
echo ===============================
echo   Test finished
echo ===============================
echo.
echo Results were written to a new folder under:
echo   data\output\competitor_probes\revzilla\
echo.
echo Open the newest folder in there and look at:
echo   probe_summary.csv  - one row per part, with the price found
echo   probe_review.txt   - a plain summary of how well it worked
echo.
echo Send those two files to Claude and we can tune it from there.
echo.
pause
