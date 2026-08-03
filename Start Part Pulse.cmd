@echo off
title Part Pulse Launcher
cd /d "%~dp0"
set "PP_ROOT=%CD%"

echo ===============================
echo   Starting Part Pulse
echo ===============================
echo.
echo Folder: %PP_ROOT%
echo.

echo [1 of 5] Checking for updates...
where git >nul 2>&1
if errorlevel 1 (
  echo   Skipped - Git is not installed. Part Pulse will still start.
) else (
  git pull origin main
  if errorlevel 1 (
    echo.
    echo   Could not update automatically. Part Pulse will still start
    echo   using the version already on this computer.
  ) else (
    echo   Up to date.
  )
)
echo.

for /f "delims=" %%v in ('git rev-parse --short HEAD 2^>nul') do set "PP_VER=%%v"
if defined PP_VER echo   Program version: %PP_VER%
echo.

echo [2 of 5] Checking Python setup...
if not exist ".venv\Scripts\python.exe" (
  echo   First-time setup. This will take a few minutes...
  py -m venv .venv
  if errorlevel 1 (
    echo.
    echo   ERROR: Could not create the Python environment.
    echo   Make sure Python is installed from python.org.
    echo.
    pause
    exit /b 1
  )
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  ".venv\Scripts\python.exe" -m playwright install chromium
  echo   Setup complete.
) else (
  echo   Ready.
)
echo.

echo [3 of 5] Stopping anything already running...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'python' -and $_.CommandLine -like ('*' + $env:PP_ROOT + '*') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1
".venv\Scripts\python.exe" clear_stuck_jobs.py
echo   Done.
echo.

echo [4 of 5] Starting the Browser Helper...
if not exist "data\private\local_collector_agent.json" (
  echo   NOT SET UP YET - skipping.
  echo   Price checks will not run until you double-click
  echo   "Setup Part Pulse Collector.cmd" one time.
) else (
  start "Part Pulse Browser Helper" /min "%PP_ROOT%\.venv\Scripts\python.exe" local_collector_agent.py --config "data\private\local_collector_agent.json"
  echo   Started.
)
echo.

echo [5 of 5] Starting the Part Pulse website...
start "Part Pulse Dashboard" "%PP_ROOT%\.venv\Scripts\python.exe" dashboard.py
echo   Started. Waiting for it to come up...
timeout /t 6 /nobreak >nul
start "" "http://127.0.0.1:8000"
echo.

echo ===============================
echo   Part Pulse is running.
echo ===============================
echo.
echo Your browser should now be open at:
echo   http://127.0.0.1:8000
echo.
echo Two small windows are now running in the background
echo (the Dashboard and the Browser Helper). Leave them alone
echo while you work.
echo.
echo When you are finished, double-click "Stop Part Pulse.cmd".
echo.
echo You can close THIS window now.
echo.
pause
