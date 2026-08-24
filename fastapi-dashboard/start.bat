@echo off
setlocal EnableExtensions
cd /d "%~dp0.."

rem Local-only legacy FastAPI browser dashboard for parity comparison.
call start-fastapi-dashboard.bat %*
exit /b %ERRORLEVEL%
