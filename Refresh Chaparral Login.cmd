@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" auth_bootstrap.py --competitor chaparral
pause
