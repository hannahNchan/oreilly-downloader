<#
.SYNOPSIS
    Start the local NLLB translation service.

.DESCRIPTION
    ONE uvicorn worker, always. Each worker is a separate process that loads its
    own copy of the model, so two workers means 7.2 GB of weights on a 12 GB card
    that is already sharing space with the Windows desktop. --workers 1 is not a
    default worth relying on here, so it is passed explicitly.

.PARAMETER Port
    Defaults to 8100. The oreilly-ingest app owns 8000.

.PARAMETER Reload
    Auto-restart on code changes. Costs a full model reload (~20 s) every time
    you save, so it is off unless you ask.

.EXAMPLE
    .\run.ps1
    .\run.ps1 -Port 8123
#>
[CmdletBinding()]
param(
    [int]$Port = 8100,
    [string]$ModelDir = "D:\ollama\models\nllb-200-3.3B-ct2-int8",
    [switch]$Reload
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "No .venv here. Run .\setup.ps1 first." -ForegroundColor Red
    exit 1
}

$env:NLLB_MODEL_DIR = $ModelDir
$env:NLLB_PORT = "$Port"

# Loopback only. The service has no authentication and is not meant to be
# reachable from anywhere else on the network.
$arguments = @(
    "-m", "uvicorn", "app.main:app",
    "--host", "127.0.0.1",
    "--port", "$Port",
    "--workers", "1"
)
if ($Reload) { $arguments += "--reload" }

Write-Host "NLLB translation service" -ForegroundColor Cyan
Write-Host "  model : $ModelDir"
Write-Host "  url   : http://127.0.0.1:$Port"
Write-Host "  health: http://127.0.0.1:$Port/health"
Write-Host "  docs  : http://127.0.0.1:$Port/docs"
Write-Host ""
Write-Host "First start takes ~20 s: 3.5 GB of weights plus a warm-up sentence." -ForegroundColor DarkGray
Write-Host "Ctrl+C to stop." -ForegroundColor DarkGray
Write-Host ""

& $venvPython @arguments
