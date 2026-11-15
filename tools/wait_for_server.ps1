param(
    [string]$HealthUrl = "http://127.0.0.1:8000/healthz",
    [string]$BrowserUrl = "http://127.0.0.1:8000",
    [int]$TimeoutSeconds = 15,
    [switch]$OpenBrowser
)

# Активна перевірка готовності: чекаємо HTTP 200 від /healthz (до 15 с),
# і лише потім відкриваємо браузер — це прибирає ERR_CONNECTION_REFUSED.
$ErrorActionPreference = "SilentlyContinue"
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$ready = $false

while ((Get-Date) -lt $deadline) {
    try {
        $response = Invoke-WebRequest -Uri $HealthUrl -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
        if ($response.StatusCode -eq 200) {
            $ready = $true
            break
        }
    } catch {
        # сервер ще стартує — продовжуємо опитування
    }
    Start-Sleep -Milliseconds 400
}

if (-not $ready) {
    Write-Output "[PaceLab] Сервер не відповів на $HealthUrl за $TimeoutSeconds с — перевір логи uvicorn вище."
    exit 1
}

Write-Output "[PaceLab] Сервер готовий (healthz 200)."

if ($OpenBrowser -and $env:RUNTRAKER_NO_BROWSER -ne "1") {
    try {
        Start-Process $BrowserUrl
        Write-Output "[PaceLab] Відкриваю $BrowserUrl у браузері."
    } catch {
        Write-Output "[PaceLab] Не вдалося відкрити браузер: $($_.Exception.Message)"
    }
}

exit 0

