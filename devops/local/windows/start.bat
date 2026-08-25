@echo off
setlocal EnableExtensions

rem Default local entry point: React Portal MFE with the shared FastAPI API.
rem The FastAPI comparison UI remains available through
rem backend\legacy-ui\start.bat when it is explicitly needed.
call "%~dp0start-react-dashboard.bat" %*
exit /b %ERRORLEVEL%
