# Loads .env into the current session, then starts the Streamlit dashboard
# in the background (logged to file) and the collector (pmr run) in the
# foreground, so its output is what you see live in this terminal.
#
# Running the collector as the actual foreground console process (rather
# than via Start-Process -NoNewWindow) means Ctrl+C hits it directly and
# reliably, instead of depending on a `finally` block that Windows doesn't
# always run on a console break. `taskkill /T` on the way out kills each
# tool's stub-exe + worker-child pair as one unit, so nothing is left behind.

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

$streamlitExe = Join-Path $repoRoot ".venv\Scripts\streamlit.exe"
$dashboardLog = Join-Path $repoRoot "dashboard.log"
$dashboardErrLog = Join-Path $repoRoot "dashboard.err.log"

$dashboard = Start-Process -FilePath $streamlitExe `
    -ArgumentList "run", "$repoRoot\apps\dashboard\Home.py" `
    -NoNewWindow -PassThru `
    -RedirectStandardOutput $dashboardLog `
    -RedirectStandardError $dashboardErrLog

Write-Host "Dashboard (streamlit) arrancado en background, PID $($dashboard.Id). Logs: $dashboardLog" -ForegroundColor Green

$pmrExe = Join-Path $repoRoot ".venv\Scripts\pmr.exe"

function Stop-Dashboard {
    if (-not $dashboard.HasExited) {
        Write-Host "Deteniendo dashboard (PID $($dashboard.Id))..." -ForegroundColor Yellow
        taskkill /PID $dashboard.Id /T /F | Out-Null
    }
}

trap {
    Stop-Dashboard
    break
}

try {
    & $pmrExe run
}
finally {
    Stop-Dashboard
}
