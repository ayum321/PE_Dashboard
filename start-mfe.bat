@echo off
setlocal
cd /d "%~dp0"

set "NODE_DIR=C:\nvm4w\nodejs"
if exist "%NODE_DIR%\npm.cmd" set "PATH=%NODE_DIR%;%PATH%"

if not exist "pe-dashboard-mfe\node_modules" (
  echo MFE dependencies are not installed.
  echo Run: cd pe-dashboard-mfe ^&^& npm ci
  exit /b 1
)

echo Starting FastAPI backend with start.bat...
start "PE Dashboard API" cmd /c "set NOPAUSE=1 ^& start.bat"
echo Starting React MFE on http://localhost:3000 ...
cd pe-dashboard-mfe
call npm run start:standalone
