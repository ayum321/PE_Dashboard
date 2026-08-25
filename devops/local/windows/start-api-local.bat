@echo off
setlocal EnableExtensions
cd /d "%~dp0..\..\.."

rem Local development serves both browser views from one shared API process.
rem Production Docker explicitly sets PE_UI_MODE=api and never ships legacy assets.
if "%PE_UI_MODE%"=="" set "PE_UI_MODE=dual"

rem Local API only. This does not open or serve the retired FastAPI dashboard UI.
if exist ".venv\Scripts\python.exe" (
  set "PYTHON_CMD=.venv\Scripts\python.exe"
) else (
  py -3.14 --version >nul 2>&1
  if not errorlevel 1 (
    set "PYTHON_CMD=py -3.14"
  ) else (
    py -3.13 --version >nul 2>&1
    if not errorlevel 1 set "PYTHON_CMD=py -3.13"
  )
)

if not defined PYTHON_CMD (
  echo ERROR: Python 3.13+ was not found. Install Python or create .venv first.
  pause
  exit /b 1
)

call %PYTHON_CMD% -c "import uvicorn" >nul 2>&1
if errorlevel 1 (
  echo Installing FastAPI API prerequisites from backend\PE_Dashboard_API\configuration\requirements.txt ...
  if not exist "backend\PE_Dashboard_API\configuration\requirements.txt" (
    echo ERROR: backend\PE_Dashboard_API\configuration\requirements.txt is missing.
    pause
    exit /b 1
  )
  call %PYTHON_CMD% -m pip install -r backend\PE_Dashboard_API\configuration\requirements.txt
  if errorlevel 1 (
    echo ERROR: FastAPI dependency installation failed.
    echo Check your Python installation, network/proxy access, then run this file again.
    pause
    exit /b 1
  )
  call %PYTHON_CMD% -c "import uvicorn" >nul 2>&1
  if errorlevel 1 (
    echo ERROR: uvicorn is still unavailable after installation.
    pause
    exit /b 1
  )
)

echo PE Audit API local mode
echo   API: http://127.0.0.1:8765
echo   This window supplies upload, SLA, findings, and Azure API calls for the React MFE.
echo   Press Ctrl+C to stop the local API.
echo.

call %PYTHON_CMD% -m uvicorn main:app --app-dir backend\PE_Dashboard_API\app --host 127.0.0.1 --port 8765
set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo API stopped with exit code %EXIT_CODE%.
pause
exit /b %EXIT_CODE%
