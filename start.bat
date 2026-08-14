@echo off
REM Windows でダブルクリックして起動するための入口。
cd /d "%~dp0"
py -3 start.py 2>nul || python start.py
pause
