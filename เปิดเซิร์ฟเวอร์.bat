@echo off
REM ==================================================================
REM  Saraban web server launcher
REM
REM  All Thai text lives in show_url.py because cmd.exe reads .bat
REM  files using the legacy ANSI codepage, not UTF-8 - Thai characters
REM  here would be garbled and executed as broken commands.
REM  Keep this file ASCII-only.
REM ==================================================================
cd /d "%~dp0"
title Saraban - web server (do not close)
cls
python show_url.py
echo.
REM  Open the browser once the server is actually listening.
REM  Runs in the background so it does not block the server.
start /b "" python show_url.py --open
python -m uvicorn web.main:app --host 0.0.0.0 --port 8000
echo.
python show_url.py --stopped
pause
