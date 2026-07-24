@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" local_collector_agent.py --config "data\private\local_collector_agent.json"
echo.
echo Desktop Collector stopped. If this window shows an error, send it to Codex.
pause
