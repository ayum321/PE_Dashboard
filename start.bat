@echo off
setlocal EnableExtensions
title PE Audit Dashboard

echo ===================================================================
echo           Performance Engineering (PE) Audit Dashboard
echo ===================================================================
echo.

set "REPO_ROOT=%~dp0"
cd /d "%REPO_ROOT%"

echo [1/3] Checking environment prerequisites...
where python >nul 2>nul
if errorlevel 1 (
  where py >nul 2>nul
  if errorlevel 1 (
    echo [!] WARNING: Python was not found on your PATH.
    echo     Please install Python 3.11+ from https://www.python.org/
    echo.
  )
)

where node >nul 2>nul
if errorlevel 1 (
  echo [!] WARNING: Node.js was not found on your PATH.
  echo     Please install Node.js 18+ LTS from https://nodejs.org/
  echo.
)

echo [2/3] Initializing Backend API (FastAPI) and Frontend UI (React MFE)...
echo.
call "%REPO_ROOT%frontend\start.bat"
if errorlevel 1 (
  echo.
  echo [!] Dashboard launcher exited with code %ERRORLEVEL%.
  pause
)
