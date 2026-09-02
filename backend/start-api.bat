@echo off
setlocal EnableExtensions
set "REPO_ROOT=%~dp0.."
cd /d "%REPO_ROOT%"

rem Local development serves both browser views from one shared API process.
rem Production Docker explicitly sets PE_UI_MODE=api and never ships legacy assets.
if "%PE_UI_MODE%"=="" (
  if exist "%REPO_ROOT%\backend\PE_Dashboard_API\app\mfe\index.html" (
    set "PE_UI_MODE=bundled_mfe"
  ) else (
    set "PE_UI_MODE=dual"
  )
)

rem Search for Python across all common environments
if exist "%REPO_ROOT%\.venv\Scripts\python.exe" (
  set "PYTHON_CMD=%REPO_ROOT%\.venv\Scripts\python.exe"
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
  echo [ERROR] Python was not found on your system.
  echo Please install Python 3.10+ from https://www.python.org/
  echo Make sure to check 'Add python.exe to PATH' during installation.
  echo.
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

rem Run from the deployable API package, not repository root.  The repository
rem deliberately retains the legacy FastAPI comparison source at root; running
rem from root makes Python resolve its `routers` package before the API package
rem and can silently return the legacy report renderer.
cd /d "%REPO_ROOT%\backend\PE_Dashboard_API\app"
rem --reload watches this directory tree and restarts the worker automatically
rem on every saved .py change, so local edits take effect without manually
rem killing/relaunching this window. Production (Dockerfile CMD) intentionally
rem does NOT use --reload -- containers are rebuilt and redeployed per release,
rem so file-watching there would be pure overhead, not a benefit.
call %PYTHON_CMD% -m uvicorn main:app --host 127.0.0.1 --port 8765 --reload
set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo API stopped with exit code %EXIT_CODE%.
pause
exit /b %EXIT_CODE%
