@echo off
setlocal EnableExtensions
title PE Audit Dashboard

echo ===================================================================
echo           Performance Engineering (PE) Audit Dashboard
echo ===================================================================
echo.

set "REPO_ROOT=%~dp0"
cd /d "%REPO_ROOT%"

rem [1/3] Detect Python Environment
set "PYTHON_CMD="
if exist "%REPO_ROOT%.venv\Scripts\python.exe" (
  set "PYTHON_CMD=%REPO_ROOT%.venv\Scripts\python.exe"
) else (
  where python >nul 2>nul
  if not errorlevel 1 (
    set "PYTHON_CMD=python"
  ) else (
    where py >nul 2>nul
    if not errorlevel 1 (
      set "PYTHON_CMD=py -3"
    ) else (
      where python3 >nul 2>nul
      if not errorlevel 1 (
        set "PYTHON_CMD=python3"
      ) else (
        py -3.14 --version >nul 2>&1
        if not errorlevel 1 (
          set "PYTHON_CMD=py -3.14"
        ) else (
          py -3.13 --version >nul 2>&1
          if not errorlevel 1 (
            set "PYTHON_CMD=py -3.13"
          ) else (
            py -3.12 --version >nul 2>&1
            if not errorlevel 1 (
              set "PYTHON_CMD=py -3.12"
            ) else (
              py -3.11 --version >nul 2>&1
              if not errorlevel 1 set "PYTHON_CMD=py -3.11"
            )
          )
        )
      )
    )
  )
)

if not defined PYTHON_CMD (
  echo.
  echo [!] Python was not found on your system.
  echo.
  echo     Please install Python 3.10+ from:
  echo     https://www.python.org/downloads/
  echo.
  echo     IMPORTANT: During installation, make sure to check
  echo     "[X] Add python.exe to PATH"
  echo.
  pause
  exit /b 1
)

echo [OK] Python detected: %PYTHON_CMD%
for /f "delims=" %%V in ('%PYTHON_CMD% --version') do echo      Version: %%V

rem [2/3] Check Dependencies
echo.
echo [2/3] Verifying dependencies...
call %PYTHON_CMD% -c "import uvicorn, fastapi, pandas" >nul 2>&1
if errorlevel 1 (
  echo Installing required Python packages...
  call %PYTHON_CMD% -m pip install -r "%REPO_ROOT%backend\PE_Dashboard_API\configuration\requirements.txt"
  if errorlevel 1 (
    echo.
    echo [ERROR] Failed to install Python dependencies.
    echo Check internet connection or run: pip install -r requirements.txt
    echo.
    pause
    exit /b 1
  )
)
echo [OK] Dependencies verified.

rem [3/3] Launch Dashboard
echo.
echo [3/3] Starting PE Dashboard Server...
echo.
echo ===================================================================
echo   Dashboard URL: http://127.0.0.1:8765
echo   API Health:    http://127.0.0.1:8765/api/health
echo ===================================================================
echo.
echo Keep this window open while using the dashboard.
echo Press Ctrl+C to stop the server.
echo.

rem Auto-launch browser once server responds
start "" /b powershell -NoProfile -WindowStyle Hidden -Command "=(Get-Date).AddSeconds(30); while((Get-Date) -lt ){ try { =Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 'http://127.0.0.1:8765/api/health'; if(.StatusCode -eq 200){ Start-Process 'http://127.0.0.1:8765'; break } } catch {}; Start-Sleep -Seconds 1 }"

set "PE_UI_MODE=bundled_mfe"
cd /d "%REPO_ROOT%backend\PE_Dashboard_API\app"
call %PYTHON_CMD% -m uvicorn main:app --host 127.0.0.1 --port 8765

if errorlevel 1 (
  echo.
  echo [!] Server stopped with exit code %ERRORLEVEL%.
  pause
)
