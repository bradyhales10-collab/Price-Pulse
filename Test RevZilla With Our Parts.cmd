@echo off
title Test RevZilla With Our Parts
cd /d "%~dp0"

echo ===============================
echo   Test RevZilla With Our Parts
echo ===============================
echo.
echo This tests RevZilla against YOUR OWN best-selling parts, instead of
echo parts picked by hand. It answers the real question: of the parts we
echo actually sell, how many does RevZilla stock and price?
echo.
echo Nothing is saved to your database and no prices are changed.
echo A browser window will open and move on its own. Please do not click
echo inside it while it runs.
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

echo [1 of 3] Getting the latest version...
where git >nul 2>&1
if errorlevel 1 (
  echo   WARNING: Git is not installed, so this may test an old version.
) else (
  git pull origin main
  if errorlevel 1 (
    echo.
    echo   WARNING: Could not update. Run "Repair Part Pulse.cmd" first
    echo   if the results look wrong.
    echo.
  )
)
for /f "delims=" %%v in ('git rev-parse --short HEAD 2^>nul') do set "PP_VER=%%v"
echo   Version: %PP_VER%
echo.

echo [2 of 3] Choosing our best-selling parts that RevZilla might carry...
".venv\Scripts\python.exe" export_probe_input.py --competitor revzilla --max-parts 15
if errorlevel 1 (
  echo.
  echo Could not build the parts list. If it says there is no database,
  echo upload a parts file in Part Pulse first, then run this again.
  echo.
  pause
  exit /b 1
)
echo.

echo [3 of 3] Checking those parts on RevZilla...
echo   This waits 1 second between parts.
echo.
".venv\Scripts\python.exe" probe_competitor.py --competitor revzilla --file "data\input\revzilla_own_catalog_probe.csv" --max-parts 15 --delay-seconds 1

echo.
echo ===============================
echo   Test finished
echo ===============================
echo.
echo Results are in a new folder under:
echo   data\output\competitor_probes\revzilla\
echo.
echo Open the newest folder and send Claude these two files:
echo   probe_summary.csv
echo   probe_review.txt
echo.
echo The line to look for is "Unavailable-listing rate". If most of our
echo parts are out of stock at RevZilla, they are not a useful competitor
echo no matter how well the price reading works.
echo.
pause
