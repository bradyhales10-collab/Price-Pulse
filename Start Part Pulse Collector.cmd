@echo off
title Part Pulse Desktop Collector
cd /d "%~dp0"
echo Part Pulse Desktop Collector
echo ============================
echo.
echo Folder: %CD%
echo.
if not exist ".venv\Scripts\python.exe" (
  echo ERROR: Python was not found at .venv\Scripts\python.exe
  echo Run Setup Part Pulse Collector.cmd first.
  echo.
  pause
  exit /b 1
)
if not exist "data\private\local_collector_agent.json" (
  echo ERROR: Collector setup file was not found.
  echo Run Setup Part Pulse Collector.cmd first.
  echo.
  pause
  exit /b 1
)
echo Starting collector. Leave this window open while you use Part Pulse.
echo After it says Desktop collector connected on the website, click Refresh Login again.
echo.
".venv\Scripts\python.exe" local_collector_agent.py --config "data\private\local_collector_agent.json"
set EXITCODE=%ERRORLEVEL%
echo.
echo Desktop Collector stopped with exit code %EXITCODE%.
echo If this window shows an error, send it to Codex.
echo Log file: data\output\local_bridge\local_collector_agent.log
pause
exit /b %EXITCODE%
