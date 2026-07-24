@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" auth_bootstrap.py --competitor partzilla --url "https://www.partzilla.com/product/kawasaki/41080-1514"
pause
