#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Launch the Haven dashboard.

.DESCRIPTION
    Starts the FastAPI app via `uv run python -m haven`. On first run (or with
    -Sync) it installs dependencies with `uv sync`. Once the server answers on
    /api/health it opens the dashboard in your default browser.

.PARAMETER Sync
    Force `uv sync` before launching (otherwise it only syncs if .venv is missing).

.PARAMETER Dev
    Sync the dev extras too (pytest, ruff). Implies a sync.

.PARAMETER NoBrowser
    Don't open the browser automatically.

.PARAMETER BindHost
    Override the bind host (else HAVEN_HOST from .env, else 127.0.0.1).

.PARAMETER Port
    Override the port (else HAVEN_PORT from .env, else 8765).

.EXAMPLE
    .\run.ps1
.EXAMPLE
    .\run.ps1 -Sync -Dev
.EXAMPLE
    .\run.ps1 -Port 9000 -NoBrowser
#>
[CmdletBinding()]
param(
    [switch]$Sync,
    [switch]$Dev,
    [switch]$NoBrowser,
    [string]$BindHost,
    [int]$Port
)

$ErrorActionPreference = 'Stop'

# Always operate from the project root (the script's own folder), so it works
# no matter where it's invoked from.
Set-Location -LiteralPath $PSScriptRoot

# uv must be on PATH.
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Error "uv is not on PATH. Install it with:  winget install astral-sh.uv  (then restart the terminal)."
    exit 1
}

# Resolve host/port: explicit param > .env > default. Only used so we open the
# right URL — the app itself reads HAVEN_HOST/HAVEN_PORT from .env on its own.
function Get-EnvValue([string]$key) {
    if (-not (Test-Path .env)) { return $null }
    foreach ($line in Get-Content .env) {
        if ($line -match "^\s*$key\s*=\s*(.+?)\s*$") {
            return $Matches[1].Trim().Trim('"').Trim("'")
        }
    }
    return $null
}

if (-not $BindHost) { $BindHost = (Get-EnvValue 'HAVEN_HOST'); if (-not $BindHost) { $BindHost = '127.0.0.1' } }
if (-not $Port)     { $p = (Get-EnvValue 'HAVEN_PORT'); $Port = if ($p) { [int]$p } else { 8765 } }

$displayHost = if ($BindHost -eq '0.0.0.0') { '127.0.0.1' } else { $BindHost }
$url = "http://${displayHost}:${Port}/"

# Sync dependencies if asked, or on first run (no .venv yet).
if ($Sync -or $Dev -or -not (Test-Path .venv)) {
    Write-Host "Syncing dependencies..." -ForegroundColor Cyan
    if ($Dev) { uv sync --extra dev } else { uv sync }
}

# Open the browser once the server is actually answering (poll /api/health).
if (-not $NoBrowser) {
    $healthUrl = "http://${displayHost}:${Port}/api/health"
    $null = Start-Job -Name HavenBrowser -ScriptBlock {
        param($health, $open)
        for ($i = 0; $i -lt 60; $i++) {
            try {
                $r = Invoke-WebRequest -Uri $health -UseBasicParsing -TimeoutSec 2
                if ($r.StatusCode -eq 200) { Start-Process $open; break }
            } catch { Start-Sleep -Milliseconds 500 }
        }
    } -ArgumentList $healthUrl, $url
}

Write-Host "Starting Haven on $url  (Ctrl+C to stop)" -ForegroundColor Green
try {
    uv run python -m haven
}
finally {
    # Clean up the browser-watcher job whether the server exits or is Ctrl+C'd.
    Get-Job -Name HavenBrowser -ErrorAction SilentlyContinue | Remove-Job -Force -ErrorAction SilentlyContinue
}
