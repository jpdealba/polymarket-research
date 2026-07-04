# Loads .env into the current session, then starts the collector (pmr run)
# in the background and the Streamlit dashboard in the foreground.
# Ctrl+C stops the dashboard and the background collector together.

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $repoRoot ".env"

if (-not (Test-Path $envFile)) {
    throw "No se encontro $envFile"
}

Get-Content $envFile | ForEach-Object {
    $line = $_.Trim()
    if ($line -eq "" -or $line.StartsWith("#")) { return }
    $idx = $line.IndexOf("=")
    if ($idx -lt 1) { return }
    $key = $line.Substring(0, $idx).Trim()
    $value = $line.Substring($idx + 1).Trim().Trim('"').Trim("'")
    Set-Item -Path "Env:$key" -Value $value
}

Write-Host "Variables de .env cargadas en esta sesion." -ForegroundColor Green

$pmrExe = Join-Path $repoRoot ".venv\Scripts\pmr.exe"
$collectorLog = Join-Path $repoRoot "collector.log"
$collectorErrLog = Join-Path $repoRoot "collector.err.log"

$collector = Start-Process -FilePath $pmrExe -ArgumentList "run" `
    -NoNewWindow -PassThru `
    -RedirectStandardOutput $collectorLog `
    -RedirectStandardError $collectorErrLog

Write-Host "Collector (pmr run) arrancado en background, PID $($collector.Id). Logs: $collectorLog" -ForegroundColor Green

$streamlitExe = Join-Path $repoRoot ".venv\Scripts\streamlit.exe"

try {
    & $streamlitExe run "$repoRoot\apps\dashboard\Home.py"
}
finally {
    if (-not $collector.HasExited) {
        Write-Host "Deteniendo collector (PID $($collector.Id))..." -ForegroundColor Yellow
        Stop-Process -Id $collector.Id -Force
    }
}
