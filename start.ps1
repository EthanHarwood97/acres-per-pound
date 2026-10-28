# start.ps1 — one-command quick start: venv + deps + serve + open dashboard
param(
    [switch]$Fresh  # full UK scan before serving (slow: can take a long time)
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# --- find a system python (before the venv exists) ---
$syspy = $null
foreach ($c in @("python", "py")) {
    if (Get-Command $c -ErrorAction SilentlyContinue) { $syspy = $c; break }
}
if (-not $syspy) { throw "Python not found on PATH. Install Python 3.10+ first." }

# --- create venv if missing ---
$venv = Join-Path $PSScriptRoot ".venv"
$py = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "Creating virtualenv (.venv)..." -ForegroundColor Cyan
    & $syspy -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "Failed to create virtualenv." }
}

# --- install deps only when missing or requirements.txt changed ---
$marker = Join-Path $venv ".deps-ok"
$reqs = Join-Path $PSScriptRoot "requirements.txt"
if (-not (Test-Path $marker) -or (Get-Item $reqs).LastWriteTime -gt (Get-Item $marker).LastWriteTime) {
    Write-Host "Installing dependencies..." -ForegroundColor Cyan
    & $py -m pip install -q -r $reqs
    if ($LASTEXITCODE -ne 0) { throw "pip install failed." }
    New-Item $marker -ItemType File -Force | Out-Null
}

# --- read host/port from config.json ---
$cfg = Get-Content (Join-Path $PSScriptRoot "config.json") -Raw | ConvertFrom-Json
$url = "http://$($cfg.server.host):$($cfg.server.port)"

# --- background job: wait for the server, then open the dashboard in a browser ---
Start-Job -ArgumentList $url -ScriptBlock {
    param($url)
    $ok = $false
    for ($i = 0; $i -lt 600; $i++) {
        try {
            Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2 | Out-Null
            $ok = $true; break
        } catch { Start-Sleep -Seconds 3 }
    }
    if ($ok) { Start-Process $url }
} | Out-Null

Write-Host ""
if ($Fresh) {
    Write-Host "Starting FRESH scan + serve (this can take a long time)..." -ForegroundColor Yellow
    Write-Host "Dashboard will open automatically at $url when the scan finishes." -ForegroundColor Yellow
    & $py -m acres_per_pound.cli serve
} else {
    Write-Host "Starting dashboard (quick mode, last scanned data)..." -ForegroundColor Green
    Write-Host "Dashboard opening at $url  (Ctrl+C to stop)" -ForegroundColor Green
    Write-Host "Tip: run '.\start.ps1 -Fresh' to rescan all UK regions first." -ForegroundColor DarkGray
    & $py -m acres_per_pound.cli serve --quick
}
