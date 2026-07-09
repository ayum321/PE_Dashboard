@echo off
REM ================================================================
REM   PE Audit Dashboard  --  Smart Self-Healing Launcher
REM   Phases 2-8: Vision AI, SLA Matrix, Benchmark, Azure Monitor
REM
REM   Capabilities:
REM     * Finds Python 3.11+ in 30+ locations (PATH, LOCALAPPDATA,
REM       ProgramFiles, WindowsApps, deep scan, winget auto-install)
REM     * Bootstraps pip with ensurepip + get-pip.py fallback
REM     * Installs packages with --user + trusted-host proxy fallback
REM     * 7-day stamp skips re-install for fast subsequent launches
REM     * Verifies all app files + uvicorn importable before starting
REM     * Finds a free port and clears any conflict automatically
REM ================================================================

setlocal ENABLEEXTENSIONS ENABLEDELAYEDEXPANSION

set "HOST=127.0.0.1"
set "APP=main:app"
set "PORT="
set "PY="

cd /d "%~dp0"

echo.
echo  ================================================================
echo   PE Audit Dashboard  ^|  Batch Performance Intelligence
echo  ================================================================
echo   Working dir : %CD%
echo.


REM ================================================================
REM  STEP 0 -- Check for local .venv first (preferred)
REM ================================================================
set "USING_VENV=0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sys;sys.exit(0 if sys.version_info>=(3,11) else 1)" >nul 2>&1
    if not errorlevel 1 (
        set "PY=.venv\Scripts\python.exe"
        set "USING_VENV=1"
        echo   [venv] Using local virtual environment: .venv
        goto :found_python
    )
)


REM ================================================================
REM  STEP 1 -- Find Python 3.11+
REM  A  py launcher (3.14 down to 3.11)
REM  B  PATH: python3, python
REM  C  LOCALAPPDATA non-PATH layouts
REM  D  ProgramFiles system-wide
REM  E  USERPROFILE alternate layouts
REM  F  Microsoft Store WindowsApps shims
REM  G  Deep filesystem scan
REM  H  winget auto-install
REM  I  Instructions + exit
REM ================================================================
echo   [1/7] Locating Python 3.11+ ...

REM A -- py launcher
where py >nul 2>&1
if not errorlevel 1 (
    for %%V in (3.14 3.13 3.12 3.11) do (
        if "!PY!"=="" (
            py -%%V --version >nul 2>&1
            if not errorlevel 1 (
                py -%%V -c "import sys;sys.exit(0 if sys.version_info>=(3,11) else 1)" >nul 2>&1
                if not errorlevel 1 set "PY=py -%%V"
            )
        )
    )
)
if not "!PY!"=="" goto :found_python

REM B -- PATH
for %%C in (python3 python) do (
    if "!PY!"=="" (
        where %%C >nul 2>&1
        if not errorlevel 1 (
            %%C -c "import sys;sys.exit(0 if sys.version_info>=(3,11) else 1)" >nul 2>&1
            if not errorlevel 1 set "PY=%%C"
        )
    )
)
if not "!PY!"=="" goto :found_python

REM C -- LOCALAPPDATA non-PATH layouts
call :try "%LOCALAPPDATA%\Python\bin\python.exe"
call :try "%LOCALAPPDATA%\Python\python.exe"
call :try "%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
call :try "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
call :try "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
call :try "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
call :try "%APPDATA%\..\Local\Python\bin\python.exe"
call :try "%APPDATA%\..\Local\Programs\Python\Python314\python.exe"
call :try "%APPDATA%\..\Local\Programs\Python\Python313\python.exe"
call :try "%APPDATA%\..\Local\Programs\Python\Python312\python.exe"
call :try "%APPDATA%\..\Local\Programs\Python\Python311\python.exe"
if not "!PY!"=="" goto :found_python

REM D -- ProgramFiles system-wide
for %%D in ("%ProgramFiles%" "%ProgramFiles(x86)%") do (
    for %%V in (Python314 Python313 Python312 Python311) do (
        call :try "%%~D\%%V\python.exe"
    )
)
if not "!PY!"=="" goto :found_python

REM E -- USERPROFILE alternate layouts
call :try "%USERPROFILE%\Python314\python.exe"
call :try "%USERPROFILE%\Python313\python.exe"
call :try "%USERPROFILE%\Python312\python.exe"
call :try "%USERPROFILE%\Python311\python.exe"
call :try "%USERPROFILE%\AppData\Local\Python\bin\python.exe"
if not "!PY!"=="" goto :found_python

REM F -- Microsoft Store / WindowsApps
for %%V in (3.14 3.13 3.12 3.11) do (
    if "!PY!"=="" (
        set "_ms=%LOCALAPPDATA%\Microsoft\WindowsApps\python%%V.exe"
        if exist "!_ms!" (
            "!_ms!" -c "import sys;sys.exit(0 if sys.version_info>=(3,11) else 1)" >nul 2>&1
            if not errorlevel 1 set "PY=!_ms!"
        )
    )
)
if not "!PY!"=="" goto :found_python

REM G -- Deep scan under common roots
echo        Scanning filesystem (may take a moment)...
for %%D in ("%LOCALAPPDATA%\Programs" "%LOCALAPPDATA%" "%ProgramFiles%" "%ProgramFiles(x86)%" "%USERPROFILE%") do (
    if "!PY!"=="" (
        for /f "delims=" %%F in ('dir /b /s "%%~D\python.exe" 2^>nul ^| findstr /i "python3"') do (
            if "!PY!"=="" (
                "%%F" -c "import sys;sys.exit(0 if sys.version_info>=(3,11) else 1)" >nul 2>&1
                if not errorlevel 1 set "PY=%%F"
            )
        )
    )
)
if not "!PY!"=="" goto :found_python

REM H -- winget auto-install
echo.
echo        Python 3.11+ not found anywhere. Attempting winget install...
where winget >nul 2>&1
if not errorlevel 1 (
    echo        Running: winget install Python.Python.3.13
    echo        (Internet required -- may take 1-3 minutes)
    winget install --id Python.Python.3.13 --source winget --accept-package-agreements --accept-source-agreements --silent
    if not errorlevel 1 (
        echo        Installed. Refreshing PATH...
        for /f "skip=2 tokens=2*" %%A in ('reg query "HKCU\Environment" /v PATH 2^>nul') do set "PATH=!PATH!;%%B"
        for /f "skip=2 tokens=2*" %%A in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v PATH 2^>nul') do set "PATH=!PATH!;%%B"
        call :try "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
        where python >nul 2>&1
        if not errorlevel 1 (
            python -c "import sys;sys.exit(0 if sys.version_info>=(3,11) else 1)" >nul 2>&1
            if not errorlevel 1 set "PY=python"
        )
        if not "!PY!"=="" goto :found_python
        echo        winget installed OK but Python unreachable in this session.
        echo        Close this window, open a NEW Command Prompt, re-run start.bat.
        echo.
    ) else (
        echo        winget install failed. Check internet connection.
    )
) else (
    echo        winget not available on this machine.
)

REM I -- Instructions + exit
echo.
echo  +==============================================================+
echo  ^|  ACTION REQUIRED: Python 3.11+ not found                    ^|
echo  +==============================================================+
echo  ^|                                                              ^|
echo  ^|  OPTION 1 -- winget (Windows 10/11, no admin needed):       ^|
echo  ^|    1. Open a NEW Command Prompt                             ^|
echo  ^|    2. Run:  winget install Python.Python.3.13               ^|
echo  ^|    3. Close all cmd windows and re-run start.bat            ^|
echo  ^|                                                              ^|
echo  ^|  OPTION 2 -- Manual installer:                              ^|
echo  ^|    1. https://www.python.org/downloads/                     ^|
echo  ^|    2. Download Python 3.13 Windows installer                ^|
echo  ^|    3. CHECK the box: "Add Python to PATH"                   ^|
echo  ^|    4. Close all cmd windows and re-run start.bat            ^|
echo  ^|                                                              ^|
echo  +==============================================================+
echo.
pause
exit /b 1

REM -- Subroutine: probe one full exe path --
:try
if "!PY!"=="" (
    if exist "%~1" (
        "%~1" -c "import sys;sys.exit(0 if sys.version_info>=(3,11) else 1)" >nul 2>&1
        if not errorlevel 1 set "PY=%~1"
    )
)
goto :eof


:found_python
echo        Found : !PY!
!PY! --version

REM If using .venv, skip pip bootstrap and package install (already managed)
if "!USING_VENV!"=="1" (
    echo.
    echo   [2/7] pip ... venv (skip)
    echo   [3/7] Packages ... venv (skip)
    goto :step4
)


REM ================================================================
REM  STEP 2 -- Ensure pip is available, bootstrap if missing
REM ================================================================
echo.
echo   [2/7] Checking pip...

!PY! -m pip --version >nul 2>&1
if not errorlevel 1 goto :pip_ok

echo        pip not found -- bootstrapping with ensurepip...
!PY! -m ensurepip --upgrade >nul 2>&1
!PY! -m pip --version >nul 2>&1
if not errorlevel 1 goto :pip_ok

echo        ensurepip failed -- trying get-pip.py download...
where curl >nul 2>&1
if not errorlevel 1 (
    curl -sS "https://bootstrap.pypa.io/get-pip.py" -o "%TEMP%\get-pip.py" 2>nul
    if exist "%TEMP%\get-pip.py" (
        !PY! "%TEMP%\get-pip.py" --quiet >nul 2>&1
        del "%TEMP%\get-pip.py" >nul 2>&1
    )
)

!PY! -m pip --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo   [ERROR] pip cannot be initialised.
    echo          Try manually: !PY! -m ensurepip --upgrade
    echo          Then re-run start.bat
    pause
    exit /b 1
)

:pip_ok
echo        pip OK


REM ================================================================
REM  STEP 3 -- Install packages
REM
REM  .pkg_stamp: skips installs when stamp < 7 days old.
REM  Set FORCE_INSTALL=1 to always re-install.
REM
REM  Per-group install cascade:
REM    1. Standard global install
REM    2. --user   (restricted / no-admin machines)
REM    3. --user + --trusted-host   (corporate TLS proxy)
REM ================================================================
echo.
echo   [3/7] Checking packages...

set "STAMP=%~dp0.pkg_stamp"

if "%FORCE_INSTALL%"=="1" goto :do_install
if not exist "!STAMP!" goto :do_install
!PY! -c "import os,time;age=(time.time()-os.path.getmtime(r'!STAMP!'))/86400;exit(0 if age<7 else 1)" >nul 2>&1
if errorlevel 1 goto :do_install
echo        Packages verified recently (stamp under 7 days). Skipping install.
echo        Delete .pkg_stamp or set FORCE_INSTALL=1 to force reinstall.
goto :step4

:do_install
echo        Installing / upgrading all packages...

REM Upgrade pip itself
!PY! -m pip install --upgrade pip --quiet --no-warn-script-location >nul 2>&1

REM Core framework
call :pkg "fastapi>=0.111.0" "uvicorn[standard]>=0.29.0" "pydantic>=2.7.0" "jinja2>=3.1.3" "python-multipart>=0.0.9"

REM Document parsing
call :pkg "pypdf>=4.2.0" "python-docx>=1.1.0" "lxml>=5.2.0" "beautifulsoup4>=4.12.3" "pdfplumber>=0.11.0"

REM Batch analytics
call :pkg "pandas>=2.2.0" "numpy>=1.26.0" "openpyxl>=3.1.2" "xlrd>=2.0.1"

REM Gemini AI (new SDK primary + legacy fallback)
call :pkg "google-genai>=1.0.0" "google-generativeai>=0.8.0"

REM Vision / image pipeline
call :pkg "PyMuPDF>=1.23.0" "Pillow>=10.0.0"

REM HTTP client
call :pkg "requests>=2.32.0"

REM Azure Monitor live-connect (optional but preinstalled)
call :pkg "azure-identity>=1.16.0" "azure-monitor-query>=1.4.0,<2.0.0" "azure-mgmt-compute>=30.0.0" "azure-mgmt-resource>=23.0.0" "azure-mgmt-subscription>=3.0.0" "azure-mgmt-resourcegraph>=8.0.0" "msal_extensions>=1.2.0"

REM Write stamp (Python writes the file cleanly, avoids CMD redirect quirks)
!PY! -c "open(r'!STAMP!','w').write('ok')" >nul 2>&1
echo        All packages ready.
goto :step4


REM -- Package install subroutine with 3-tier fallback --
:pkg
REM Attempt 1: standard (works in venv, admin install, or writable site-packages)
!PY! -m pip install --quiet --no-warn-script-location %* >nul 2>&1
if not errorlevel 1 goto :eof

REM Attempt 2: --user (no admin / restricted corporate machine — skipped inside venv)
!PY! -c "import sys; exit(0 if hasattr(sys, 'real_prefix') or (hasattr(sys,'base_prefix') and sys.base_prefix!=sys.prefix) else 1)" >nul 2>&1
if not errorlevel 1 (
    REM Inside a virtualenv — --user is not valid; fall straight through to trusted-host attempt
    goto :pkg_proxy
)
!PY! -m pip install --quiet --no-warn-script-location --user %* >nul 2>&1
if not errorlevel 1 (
    echo        [user-mode] %*
    goto :eof
)

:pkg_proxy
REM Attempt 3: trusted-host (corporate TLS inspection proxy)
!PY! -m pip install --quiet --no-warn-script-location --trusted-host pypi.org --trusted-host files.pythonhosted.org --trusted-host pypi.python.org %* >nul 2>&1
if not errorlevel 1 (
    echo        [proxy-mode] %*
    goto :eof
)

echo   [WARN] Could not install: %*
echo          (network/proxy issue -- app may still work if already installed)
goto :eof


:step4
REM ================================================================
REM  STEP 4 -- Verify required files and uvicorn importable
REM ================================================================
echo.
echo   [4/7] Verifying app files...

set "FILES_OK=1"
for %%F in (main.py _find_port.py _open_browser.py _seed_config.py _cleanup_stale.py) do (
    if not exist "%%F" (
        echo   [ERROR] Missing: %%F
        set "FILES_OK=0"
    )
)
if "!FILES_OK!"=="0" (
    echo.
    echo          Dashboard files are incomplete.
    echo          Please re-extract PE_Dashboard_Release.zip into a fresh folder.
    pause
    exit /b 1
)

!PY! -c "import uvicorn" >nul 2>&1
if errorlevel 1 (
    echo        uvicorn not importable -- forcing reinstall...
    call :pkg "uvicorn[standard]>=0.29.0" "fastapi>=0.111.0"
    !PY! -c "import uvicorn" >nul 2>&1
    if errorlevel 1 (
        echo   [ERROR] uvicorn still unavailable after reinstall.
        echo          Check internet/proxy and re-run start.bat.
        pause
        exit /b 1
    )
)

!PY! -c "from fastapi.templating import Jinja2Templates" >nul 2>&1
if errorlevel 1 (
    echo        Jinja2 templating support missing -- forcing reinstall...
    call :pkg "jinja2>=3.1.3" "fastapi>=0.111.0"
    !PY! -c "from fastapi.templating import Jinja2Templates" >nul 2>&1
    if errorlevel 1 (
        echo   [ERROR] Jinja2 templating is still unavailable after reinstall.
        echo          Check internet/proxy and re-run start.bat.
        pause
        exit /b 1
    )
)

!PY! -c "import pandas" >nul 2>&1
if errorlevel 1 (
    echo        pandas missing -- forcing reinstall...
    call :pkg "pandas>=2.2.0" "numpy>=1.26.0" "openpyxl>=3.1.2"
    !PY! -c "import pandas" >nul 2>&1
    if errorlevel 1 (
        echo   [ERROR] pandas is still unavailable after reinstall.
        echo          Check internet/proxy and re-run start.bat.
        pause
        exit /b 1
    )
)

!PY! -c "import openpyxl" >nul 2>&1
if errorlevel 1 (
    echo        openpyxl missing -- forcing reinstall...
    call :pkg "openpyxl>=3.1.2" "xlrd>=2.0.1"
    !PY! -c "import openpyxl" >nul 2>&1
    if errorlevel 1 (
        echo   [ERROR] openpyxl is still unavailable after reinstall.
        echo          Ctrl-M / SLA Matrix .xlsx uploads will fail without it.
        echo          Check internet/proxy and re-run start.bat.
        pause
        exit /b 1
    )
)

!PY! -c "from azure.monitor.query import MetricsQueryClient" >nul 2>&1
if errorlevel 1 (
    echo        Azure Monitor SDK incomplete -- forcing reinstall...
    call :pkg "azure-identity>=1.16.0" "azure-monitor-query>=1.4.0,<2.0.0" "azure-mgmt-compute>=30.0.0" "azure-mgmt-resource>=23.0.0" "azure-mgmt-subscription>=3.0.0" "azure-mgmt-resourcegraph>=8.0.0" "msal_extensions>=1.2.0"
    !PY! -c "from azure.monitor.query import MetricsQueryClient" >nul 2>&1
    if errorlevel 1 (
        echo   [ERROR] Azure Monitor SDK is still incomplete after reinstall.
        echo          Check internet/proxy and re-run start.bat.
        pause
        exit /b 1
    )
)

!PY! -c "import pdfplumber" >nul 2>&1
if errorlevel 1 (
    echo        pdfplumber missing -- forcing reinstall...
    call :pkg "pdfplumber>=0.11.0"
    !PY! -c "import pdfplumber" >nul 2>&1
    if errorlevel 1 (
        echo   [ERROR] pdfplumber is still unavailable after reinstall.
        echo          Check internet/proxy and re-run start.bat.
        pause
        exit /b 1
    )
)

!PY! -c "import msal_extensions" >nul 2>&1
if errorlevel 1 (
    echo        msal_extensions missing -- forcing reinstall...
    call :pkg "msal_extensions>=1.2.0"
    !PY! -c "import msal_extensions" >nul 2>&1
    if errorlevel 1 (
        echo   [ERROR] msal_extensions is still unavailable after reinstall.
        echo          Check internet/proxy and re-run start.bat.
        pause
        exit /b 1
    )
)
echo        All files and imports verified.


REM ================================================================
REM  STEP 5 -- Seed config
REM ================================================================
echo.
echo   [5/7] Seeding configuration...
!PY! _seed_config.py >nul 2>&1
if errorlevel 1 echo        [WARN] Config seed error -- defaults will apply.
echo        Config ready.


REM ================================================================
REM  STEP 6 -- Secure port: kill squatters, find free port
REM
REM  1. Kill ANY process listening on candidate ports
REM  2. Wait for OS to release the socket
REM  3. Use _find_port.py (SO_EXCLUSIVEADDRUSE + double-check)
REM  4. If uvicorn still fails to bind, auto-retry next free port
REM ================================================================
echo.
echo   [6/7] Securing port...

REM Self-heal: reap stale PE Dashboard server processes left over from prior
REM runs BEFORE touching ports. The plain port scan below only catches things
REM LISTENING on a candidate port; it misses orphaned uvicorn --reload workers
REM and servers that fell back to a random ephemeral port (e.g. :60371). Those
REM are what pile up over days and choke the machine. The reaper attributes
REM processes to THIS folder (cmdline signature + working directory) and kills
REM them + their child trees. It never blocks startup (always exits 0).
echo        Reaping stale dashboard processes...
!PY! _cleanup_stale.py --quiet 2>nul

REM Kill any process listening on candidate ports
for %%Q in (8000 8765 8080 8888 9000 9090 9999 7878 5000) do (
    for /f "tokens=5" %%K in ('netstat -ano 2^>nul ^| findstr "LISTENING" ^| findstr ":%%Q "') do (
        echo        Killing PID %%K on port %%Q
        taskkill /F /PID %%K >nul 2>&1
    )
)

REM Wait for OS TCP stack to release sockets (TIME_WAIT → CLOSED)
echo        Waiting for port release...
!PY! -c "import time; time.sleep(2)" >nul 2>&1

for /f "usebackq delims=" %%P in (`!PY! _find_port.py 2^>nul`) do set "PORT=%%P"

if not "!PORT!"=="" goto :port_found
echo   [ERROR] No free port found - Python may have failed to run _find_port.py.
echo          Check Python is installed and re-run start.bat.
pause
exit /b 1

:port_found
echo        Port : !PORT!


REM ================================================================
REM  STEP 7 -- Banner, open browser, start server (with retry)
REM
REM  Server starts in the foreground (Ctrl+C to stop).
REM  If the port fails to bind (TIME_WAIT race), automatically
REM  retries on the next free port — up to 3 attempts.
REM  Browser opens in background, polls /api/health until ready
REM  and verifies service identity = "pe-audit-dashboard".
REM ================================================================

set "RETRY_COUNT=0"

:server_start
echo.
echo  ================================================================
echo   Dashboard  :  http://%HOST%:!PORT!/
echo   API Docs   :  http://%HOST%:!PORT!/docs
echo   Config     :  .pe_config.json  (edit in Settings tab)
echo.
echo   Tabs: Upload+Intake  Executive  Batch  Resource  Correlation
echo         Findings  Red Flags  SLA Matrix  Benchmark  SOW  Settings
echo  ================================================================
echo.
echo   [7/7] Starting server  (Ctrl+C to stop)...
echo.

start "PE Browser" /B !PY! _open_browser.py %HOST% !PORT!
!PY! -m uvicorn %APP% --host %HOST% --port !PORT! --reload --reload-dir routers --reload-dir services --reload-dir templates --reload-dir static

REM ── If uvicorn exits, check if it was a port-bind failure ──
set "EXIT_CODE=!ERRORLEVEL!"

REM Check if port bind failed (exit code 1 and retry available)
if "!EXIT_CODE!"=="1" (
    set /a "RETRY_COUNT+=1"
    if !RETRY_COUNT! LEQ 3 (
        echo.
        echo        [WARN] Port !PORT! bind failed — finding next free port (attempt !RETRY_COUNT!/3)...
        !PY! -c "import time; time.sleep(1)" >nul 2>&1
        for /f "usebackq delims=" %%P in (`!PY! _find_port.py 2^>nul`) do set "PORT=%%P"
        if not "!PORT!"=="" goto :server_start
    )
)

echo.
echo  ================================================================
echo   Server stopped.
echo   Re-run start.bat to restart the PE Dashboard.
echo  ================================================================
echo.
if not "%NOPAUSE%"=="1" pause
