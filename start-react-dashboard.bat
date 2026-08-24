@echo off
setlocal EnableExtensions
cd /d "%~dp0"
rem React Portal MFE plus the shared local API and optional comparison UI.
set "PE_UI_MODE=dual"
call start-mfe.bat %*
exit /b %ERRORLEVEL%
