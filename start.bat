@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"
title RunTracker 3.0

rem ── перший запуск: venv + залежності ───────────────────────────────────
if not exist ".venv\Scripts\python.exe" (
    echo [RunTracker] Створюю віртуальне середовище...
    python -m venv .venv
    if errorlevel 1 goto :setup_failed
    echo [RunTracker] Встановлюю залежності...
    ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
    if errorlevel 1 goto :setup_failed
)

rem ── локальна IP-адреса для доступу з телефона ─────────────────────────
set "LAN_IP="
for /f "usebackq delims=" %%i in (`powershell -NoProfile -ExecutionPolicy Bypass -File "tools\lan_ip.ps1"`) do set "LAN_IP=%%i"
if "%LAN_IP%"=="" set "LAN_IP=127.0.0.1"

echo ============================================================
echo   RunTracker 3.0 — персональна аналітика бігу
echo.
echo   На цьому ПК:  http://127.0.0.1:8000
echo   Для відкриття з телефона у Wi-Fi: http://%LAN_IP%:8000
echo.
echo   Сервер слухає 0.0.0.0:8000 — доступний у локальній мережі.
echo   Зупинити сервер: Ctrl+C
echo ============================================================
echo.

rem ── через 2 секунди відкрити браузер на ПК (helper, не блокує консоль) ─
start "" /b powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process 'http://127.0.0.1:8000'"

".venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

echo.
echo [RunTracker] Сервер зупинено.
pause
exit /b 0

:setup_failed
echo.
echo [RunTracker] Не вдалося підготувати середовище.
echo Перевір, що Python 3.11+ встановлений і доступний у PATH.
pause
exit /b 1
