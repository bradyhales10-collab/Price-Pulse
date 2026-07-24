@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" auth_bootstrap.py --competitor chaparral --url "https://www.chapmoto.com/search/?q=41080-1514&type=oem"
pause
