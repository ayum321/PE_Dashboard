@echo off
setlocal EnableExtensions
set "REPO_ROOT=%~dp0..\.."
cd /d "%REPO_ROOT%"

rem Local-only legacy FastAPI browser dashboard for parity comparison.
call "%REPO_ROOT%\devops\local\windows\start-fastapi-dashboard.bat" %*
exit /b %ERRORLEVEL%
