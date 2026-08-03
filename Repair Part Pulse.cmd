@echo off
title Repair Part Pulse
cd /d "%~dp0"
set "PP_ROOT=%CD%"

echo ===============================
echo   Repair Part Pulse
echo ===============================
echo.
echo Use this when Part Pulse is not working right.
echo.
echo It will:
echo   - Stop Part Pulse
echo   - Replace the program files with the latest version from GitHub
echo   - Reinstall everything it needs
echo.
echo Your data is NOT touched. Your uploaded parts, price history,
echo saved sign-ins, and database all stay exactly as they are.
echo.
pause
echo.

echo [1 of 4] Stopping Part Pulse...
taskkill /F /FI "WINDOWTITLE eq Part Pulse Browser Helper*" >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq Part Pulse Dashboard*" >nul 2>&1
if exist ".venv\Scripts\python.exe" ".venv\Scripts\python.exe" clear_stuck_jobs.py
echo   Done.
echo.

echo [2 of 4] Getting the latest version from GitHub...
where git >nul 2>&1
if errorlevel 1 (
  echo   ERROR: Git is not installed, so the program files cannot be updated.
  echo   Install Git from https://git-scm.com/download/win and run this again.
  echo.
  pause
  exit /b 1
)
git fetch origin
if errorlevel 1 (
  echo   ERROR: Could not reach GitHub. Check your internet connection.
  echo.
  pause
  exit /b 1
)
git reset --hard origin/main
if errorlevel 1 (
  echo   ERROR: Could not update the program files.
  echo.
  pause
  exit /b 1
)
echo   Done.
echo.
echo [2b of 4] Clearing old cached code...
for /f "delims=" %%d in ('dir /s /b /ad __pycache__ 2^>nul') do rmdir /s /q "%%d" 2>nul
echo   Done.
echo.

echo [3 of 4] Reinstalling what Part Pulse needs...
echo   This can take a few minutes. Please wait.
if not exist ".venv\Scripts\python.exe" (
  py -m venv .venv
  if errorlevel 1 (
    echo   ERROR: Could not create the Python environment.
    echo   Make sure Python is installed from python.org.
    echo.
    pause
    exit /b 1
  )
)
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
".venv\Scripts\python.exe" -m playwright install chromium
echo   Done.
echo.

echo [4 of 4] Checking that everything works...
".venv\Scripts\python.exe" -m pytest -q
if errorlevel 1 (
  echo.
  echo   WARNING: Some checks did not pass. Part Pulse may still work,
  echo   but send this window to Claude if you keep having trouble.
) else (
  echo   All checks passed.
)
echo.

for /f "delims=" %%v in ('git rev-parse --short HEAD 2^>nul') do set "PP_VER=%%v"
for /f "delims=" %%d in ('git log -1 --format^=%%cd --date^=short 2^>nul') do set "PP_DATE=%%d"

echo ===============================
echo   Repair finished.
echo ===============================
echo.
echo   Program version now on this computer: %PP_VER%  (%PP_DATE%)
echo.
echo If you were asked to update before running a test, the version above
echo is what you are now running. Quote it if something looks wrong.
echo.
echo Starting Part Pulse again for you...
echo.
start "" "%PP_ROOT%\Start Part Pulse.cmd"
echo.
echo A new window has opened to start Part Pulse. You can close this one.
echo.
pause
