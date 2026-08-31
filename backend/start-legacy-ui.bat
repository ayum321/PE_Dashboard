@echo off
setlocal EnableExtensions
set "REPO_ROOT=%~dp0.."
cd /d "%REPO_ROOT%"

rem Reuse the same local API as the React MFE; never start a competing server.
set "PE_UI_MODE=dual"
set "LEGACY_URL=http://127.0.0.1:8765/legacy"
powershell -NoProfile -Command "try { $r=Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 'http://127.0.0.1:8765/api/health'; if($r.StatusCode -eq 200){ exit 0 } } catch {}; exit 1" >nul 2>&1
if not errorlevel 1 (
  echo Reusing the shared local API. Opening the FastAPI comparison dashboard ...
  start "" "%LEGACY_URL%"
  exit /b 0
)

echo Starting the shared local API and FastAPI comparison dashboard at %LEGACY_URL% ...
start "" /b powershell -NoProfile -WindowStyle Hidden -Command "$deadline=(Get-Date).AddSeconds(45); while((Get-Date) -lt $deadline){ try { $r=Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 'http://127.0.0.1:8765/api/health'; if($r.StatusCode -eq 200){ Start-Process '%LEGACY_URL%'; break } } catch {}; Start-Sleep -Seconds 1 }"
call "%REPO_ROOT%\backend\start-api.bat"
exit /b %ERRORLEVEL%
