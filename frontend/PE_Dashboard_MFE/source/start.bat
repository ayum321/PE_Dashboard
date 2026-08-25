@echo off
setlocal EnableExtensions
cd /d "%~dp0.."

rem Recommended local launcher for the React Blue Yonder Portal MFE.
call start-react-dashboard.bat %*
exit /b %ERRORLEVEL%
