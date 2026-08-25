@echo off
setlocal EnableExtensions
set "REPO_ROOT=%~dp0..\..\.."
cd /d "%REPO_ROOT%"

rem Recommended local launcher for the React Blue Yonder Portal MFE.
call "%REPO_ROOT%\devops\local\windows\start-react-dashboard.bat" %*
exit /b %ERRORLEVEL%
