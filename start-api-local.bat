@echo off
setlocal EnableExtensions
cd /d "%~dp0"

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
  echo ERROR: FastAPI dependencies are unavailable in this Python environment.
  echo Run start.bat once to install local Python prerequisites, then run start-mfe.bat again.
  pause
  exit /b 1
)

echo PE Audit API local mode
echo   API: http://localhost:8765
echo   This window supplies upload, SLA, findings, and Azure API calls for the React MFE.
echo   Press Ctrl+C to stop the local API.
echo.

call %PYTHON_CMD% -m uvicorn main:app --app-dir app --host 127.0.0.1 --port 8765
set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo API stopped with exit code %EXIT_CODE%.
pause
exit /b %EXIT_CODE%
