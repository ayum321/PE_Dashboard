@echo off
REM ─────────────────────────────────────────────────────────────────────────
REM  PE Dashboard — Detached Server Launcher
REM  Double-click this file (or run from any terminal).
REM  Opens the server in its own console window — survives VS Code restarts.
REM ─────────────────────────────────────────────────────────────────────────
cd /d "%~dp0"

echo.
echo  Starting PE Dashboard server in a new window...
echo  URL: http://127.0.0.1:8765
echo.

REM Launch start.bat in a new independent console window.
REM start.bat already opens the browser via _open_browser.py once the server
REM is ready — no second open needed here (would cause duplicate tabs).
start "PE Dashboard Server" cmd /k start.bat
