@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"
title PaceLab

rem --- first run: venv + dependencies -----------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo [PaceLab] Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 goto :setup_failed
    echo [PaceLab] Installing dependencies...
    ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
    if errorlevel 1 goto :setup_failed
)

rem --- LAN IP for phone access (ExecutionPolicy Bypass) -----------------
set "LAN_IP="
for /f "usebackq delims=" %%i in (`powershell -NoProfile -ExecutionPolicy Bypass -File "tools\lan_ip.ps1" 2^>nul`) do set "LAN_IP=%%i"
if "%LAN_IP%"=="" set "LAN_IP=127.0.0.1"

rem --- Ukrainian banner is printed by a UTF-8 PowerShell helper ---------
powershell -NoProfile -ExecutionPolicy Bypass -File "tools\console_banner.ps1"

rem --- wait for /healthz (HTTP 200, up to 15 s), then open the browser --
set "BROWSER_ARG=-OpenBrowser"
if /i "%RUNTRAKER_NO_BROWSER%"=="1" set "BROWSER_ARG="
start "" /b powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "tools\wait_for_server.ps1" %BROWSER_ARG%

".venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

rem --- never close the console silently on failure ---------------------
if errorlevel 1 (
    echo.
    echo [PaceLab] ERROR: the server exited with a non-zero code.
    echo Check the error text above and fix the cause.
    pause
    exit /b 1
)

echo.
echo [PaceLab] Server stopped.
pause
exit /b 0

:setup_failed
echo.
echo [PaceLab] Failed to prepare the environment.
echo Make sure Python 3.11+ is installed and available in PATH.
pause
exit /b 1

