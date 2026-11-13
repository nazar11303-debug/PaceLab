$ErrorActionPreference = "SilentlyContinue"

function Get-PrivateIPv4 {
    $addresses = Get-NetIPAddress -AddressFamily IPv4 |
        Where-Object {
            $_.IPAddress -notlike "127.*" -and
            $_.IPAddress -notlike "169.254.*" -and
            $_.PrefixOrigin -ne "WellKnown" -and
            $_.AddressState -ne "Duplicate"
        }
    $lan = $addresses |
        Where-Object { $_.IPAddress -match "^(192\.168\.|10\.|172\.(1[6-9]|2[0-9]|3[01])\.)" } |
        Select-Object -First 1
    if (-not $lan) { $lan = $addresses | Select-Object -First 1 }
    if ($lan) { return $lan.IPAddress }
    return $null
}

function Get-IPv4FromIpconfig {
    $output = & ipconfig.exe 2>$null
    foreach ($line in $output) {
        if ($line -match "IPv4[^:]*:\s*([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)") {
            $candidate = $Matches[1]
            if ($candidate -notlike "127.*" -and $candidate -notlike "169.254.*") {
                return $candidate
            }
        }
    }
    return $null
}

$ip = Get-PrivateIPv4
if (-not $ip) { $ip = Get-IPv4FromIpconfig }
if (-not $ip) { $ip = "127.0.0.1" }
Write-Output $ip
