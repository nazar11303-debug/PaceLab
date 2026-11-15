param(
    [string]$LocalUrl = "http://127.0.0.1:8000",
    [string]$LanIp = $env:LAN_IP
)

# Друк банера українською. Окремий .ps1 (UTF-8 BOM), бо cmd.exe ненадійно
# парсить кирилицю всередині .bat файлів.
$ErrorActionPreference = "SilentlyContinue"
if ([string]::IsNullOrWhiteSpace($LanIp)) { $LanIp = "127.0.0.1" }

Write-Host "============================================================"
Write-Host "  PaceLab — персональна аналітика бігу"
Write-Host ""
Write-Host "  На цьому ПК:  $LocalUrl"
Write-Host "  Для відкриття з телефона у Wi-Fi: http://$LanIp`:8000"
Write-Host ""
Write-Host "  Сервер слухає 0.0.0.0:8000 — доступний у локальній мережі."
Write-Host "  Зупинити сервер: Ctrl+C"
Write-Host "============================================================"
Write-Host ""

