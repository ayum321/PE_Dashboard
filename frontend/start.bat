@echo off
setlocal EnableExtensions

set "REPO_ROOT=%~dp0.."

rem Local dual mode keeps the React MFE and comparison UI on one shared API.
rem Production Docker remains API-only through PE_UI_MODE=api.
set "PE_UI_MODE=dual"

rem React MFE local launcher. It deliberately does not start the legacy FastAPI dashboard.
cd /d "%REPO_ROOT%"
set "MFE_DIR=%CD%\frontend\PE_Dashboard_MFE\source"

if not exist "%MFE_DIR%\package.json" (
  echo ERROR: React MFE folder not found: "%MFE_DIR%"
  exit /b 1
)

rem Prefer the corporate NVM installation when available; otherwise use Node already on PATH.
set "NODE_DIR=C:\nvm4w\nodejs"
if exist "%NODE_DIR%\node.exe" set "PATH=%NODE_DIR%;%PATH%"

where node >nul 2>nul
if errorlevel 1 (
  echo ERROR: Node.js is required but was not found.
  echo Install the current Node LTS release, then run this file again.
  exit /b 1
)

where npm >nul 2>nul
if errorlevel 1 (
  echo ERROR: npm was not found with Node.js.
  echo Repair or reinstall the current Node LTS release, then run this file again.
  exit /b 1
)

for /f "delims=" %%V in ('node --version') do set "NODE_VERSION=%%V"
for /f "delims=" %%V in ('npm --version') do set "NPM_VERSION=%%V"
for /f "tokens=1 delims=." %%V in ('node -p "process.versions.node"') do set "NODE_MAJOR=%%V"

if not defined NODE_MAJOR (
  echo ERROR: Could not determine the installed Node.js version.
  exit /b 1
)

if %NODE_MAJOR% LSS 18 (
  echo ERROR: Node.js 18 or newer is required. Found %NODE_VERSION%.
  echo Install the current Node LTS release, then run this file again.
  exit /b 1
)

if not exist "%MFE_DIR%\node_modules\react-scripts\bin\react-scripts.js" (
  echo Installing React MFE dependencies with npm ci...
  pushd "%MFE_DIR%"
  call npm ci
  if errorlevel 1 (
    echo ERROR: Dependency installation failed. Resolve the npm error above and run again.
    popd
    exit /b 1
  )
  popd
)

rem Optional first argument: API endpoint. It is an endpoint only, never a password or secret.
set "LOCAL_API_URL=%~1"
set "LOCAL_API_ARGUMENT=%~1"
if "%LOCAL_API_URL%"=="" set "LOCAL_API_URL=http://127.0.0.1:8765"

call :ensure_api
if errorlevel 1 exit /b 1

rem Never start a second development server on the same port. Two React
rem servers can bind different loopback address families and make the browser
rem appear to serve stale code.  Check this only after the API is healthy, so
rem frontend\start.bat also repairs an API that was stopped under an existing UI.
call :ui_is_ready
if not errorlevel 1 (
  echo React MFE is already running at http://127.0.0.1:3000
  start "" "http://127.0.0.1:3000"
  exit /b 0
)

rem These values override the deployment template for this local process only.
set "appName=PE Audit Dashboard (Local)"
set "frameUrlPath=/"
rem With no explicit endpoint, dashboardApi derives the page host from
rem the browser URL. That keeps the pe_sid Azure session cookie first-party.
set "apiBaseUrl="
if not "%LOCAL_API_ARGUMENT%"=="" set "apiBaseUrl=%LOCAL_API_URL%"

echo.
echo React MFE local mode
echo   Node: %NODE_VERSION%  ^|  npm: %NPM_VERSION%
echo   UI:   http://127.0.0.1:3000
echo   API:  %LOCAL_API_URL%
echo.
echo The local audit API is ready. The React MFE will now start.
echo Press Ctrl+C to stop the React development server.
echo.

rem react-scripts has no reliable browser-launch behaviour in this MFE setup.
rem Open the dashboard only after the server answers, avoiding a browser
rem connection-refused page during initial compilation.
start "" /b powershell -NoProfile -WindowStyle Hidden -Command "$deadline=(Get-Date).AddSeconds(45); while((Get-Date) -lt $deadline){ try { $r=Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 'http://127.0.0.1:3000'; if($r.StatusCode -eq 200){ Start-Process 'http://127.0.0.1:3000'; break } } catch {}; Start-Sleep -Seconds 1 }"

pushd "%MFE_DIR%"
call npm run start:standalone
set "EXIT_CODE=%ERRORLEVEL%"
popd
exit /b %EXIT_CODE%

:ensure_api
call :api_is_ready
if not errorlevel 1 goto :eof

if /i not "%LOCAL_API_URL%"=="http://127.0.0.1:8765" (
  echo ERROR: The configured API is not reachable: %LOCAL_API_URL%
  echo Start that API service or run frontend\start.bat with a reachable API URL.
  exit /b 1
)

echo Starting local audit API on http://127.0.0.1:8765 ...
start "PE Audit API (local)" /d "%CD%" cmd /k call "%REPO_ROOT%\backend\start-api.bat"

for /l %%R in (1,1,20) do (
  timeout /t 1 /nobreak >nul
  call :api_is_ready
  if not errorlevel 1 goto :eof
)

echo ERROR: The local audit API did not become ready within 20 seconds.
echo Check the "PE Audit API (local)" window for its startup error.
exit /b 1

:api_is_ready
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 '%LOCAL_API_URL%/api/health'; if ($r.StatusCode -eq 200) { exit 0 } } catch {}; exit 1" >nul 2>&1
exit /b %ERRORLEVEL%

:ui_is_ready
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 'http://127.0.0.1:3000'; if ($r.StatusCode -eq 200) { exit 0 } } catch {}; exit 1" >nul 2>&1
exit /b %ERRORLEVEL%
