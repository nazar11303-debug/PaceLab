@echo off
chcp 65001 >nul
cd /d "%~dp0"
title RunTracker

rem --- first run: create the virtual environment and install dependencies ---
if not exist ".venv\Scripts\python.exe" (
    echo [RunTracker] Virtual environment not found, creating it...
    python -m venv .venv
    if errorlevel 1 goto :setup_failed
    echo [RunTracker] Installing dependencies...
    ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
    if errorlevel 1 goto :setup_failed
)

rem --- open the app in the default browser (console stays open for logs) ---
start "" http://127.0.0.1:8000

echo [RunTracker] Starting server on http://127.0.0.1:8000
echo [RunTracker] Press Ctrl+C to stop the server.
echo.
".venv\Scripts\python.exe" -m uvicorn app.main:app --reload

echo.
echo [RunTracker] Server stopped.
pause
exit /b 0

:setup_failed
echo.
echo [RunTracker] Setup failed. Make sure Python 3.11+ is installed and available in PATH.
pause
exit /b 1
