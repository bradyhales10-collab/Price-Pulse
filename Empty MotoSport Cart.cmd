@echo off
title Empty MotoSport Cart
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Part Pulse is not set up on this computer yet.
  pause
  exit /b 1
)
echo Opening the MotoSport cart and clearing it.
echo A browser window will open. Do not close it until this finishes.
echo.
".venv\Scripts\python.exe" empty_competitor_cart.py motosport > cart_report.txt 2>&1
type cart_report.txt
echo.
echo A copy was saved to cart_report.txt in this folder.
echo If the cart is still not empty, send that file to Claude.
echo.
pause
