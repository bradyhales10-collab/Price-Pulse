@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" setup_local_collector_agent.py
pause
