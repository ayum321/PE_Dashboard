@echo off
REM ================================================================
REM   PE Dashboard — Development Mode (auto-reload)
REM
REM   Changes to routers/, services/, static/, templates/
REM   auto-restart the server. Just save the file and refresh
REM   the browser — no manual stop/start needed.
REM
REM   For production/shipping, use start.bat instead.
REM ================================================================
setlocal ENABLEEXTENSIONS ENABLEDELAYEDEXPANSION

set "HOST=127.0.0.1"
set "PORT=8765"

REM ── Find Python ──
set "PY="
for %%V in (3.14 3.13 3.12 3.11) do (
    if not defined PY (
        py -%%V -c "import sys" >nul 2>&1 && set "PY=py -%%V"
    )
)
if not defined PY (
    python --version >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo   [ERROR] Python not found. Install Python 3.11+ and re-run.
    pause
    exit /b 1
)

REM ── Validate JS before starting ──
echo.
echo   Validating JavaScript...
!PY! _validate_js.py
if errorlevel 1 (
    echo.
    echo   [ERROR] Fix JavaScript errors above before starting.
    pause
    exit /b 1
)

REM ── Kill any existing server on this port ──
for /f "tokens=5" %%K in ('netstat -ano 2^>nul ^| findstr "LISTENING" ^| findstr ":%PORT% "') do (
    echo   Killing PID %%K on port %PORT%
    taskkill /F /PID %%K >nul 2>&1
)
timeout /t 2 /nobreak >nul

REM ── Start with auto-reload ──
echo.
echo  ================================================================
echo   DEV MODE : http://%HOST%:%PORT%/
echo   Auto-reload watches: routers/ services/ static/ templates/
echo   Save file + refresh browser = changes live
echo  ================================================================
echo.
start "" http://%HOST%:%PORT%/
!PY! -m uvicorn main:app --host %HOST% --port %PORT% --reload --reload-dir routers --reload-dir services --reload-dir templates --reload-dir static
pause
