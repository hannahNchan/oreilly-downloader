<#
.SYNOPSIS
    Set up the local NLLB-200 translation service.

.DESCRIPTION
    Creates a venv inside services/translator (separate from the app's venv),
    installs the pinned dependencies, resolves the CUDA DLLs, checks that
    CTranslate2 really sees the GPU, and only then downloads the 3.5 GB model.

    That order is deliberate: finding out CUDA is broken after a ten minute
    download is the worst possible sequence.

.PARAMETER WithTorchCuda
    Install the PyTorch CUDA build (~2.5 GB) purely to borrow the cuBLAS and
    cuDNN DLLs it bundles in torch/lib. torch is never imported by the service.
    Use this when the lighter NVIDIA wheels are not available for Windows.

.PARAMETER SkipModel
    Do everything except the model download.

.PARAMETER IgnoreCudaCheck
    Continue even if CTranslate2 cannot see the GPU.

.PARAMETER ModelDir
    Where the model goes. Defaults to D:\ollama\models\nllb-200-3.3B-ct2-int8.

.EXAMPLE
    .\setup.ps1
    .\setup.ps1 -WithTorchCuda
#>
[CmdletBinding()]
param(
    [switch]$WithTorchCuda,
    [switch]$SkipModel,
    [switch]$IgnoreCudaCheck,
    [string]$ModelDir = "D:\ollama\models\nllb-200-3.3B-ct2-int8"
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Write-Step($text) {
    Write-Host ""
    Write-Host ("=" * 72) -ForegroundColor Cyan
    Write-Host $text -ForegroundColor Cyan
    Write-Host ("=" * 72) -ForegroundColor Cyan
}

function Assert-LastExit($what) {
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "FAILED: $what (exit $LASTEXITCODE)" -ForegroundColor Red
        exit 1
    }
}

# The child processes read this, so verify_cuda.py and download_model.py agree
# with each other and with whatever is passed on the command line.
$env:NLLB_MODEL_DIR = $ModelDir

# ---------------------------------------------------------------------------
Write-Step "1/6  Python"

$python = "python"
try {
    $version = & $python -c "import sys; print('%d.%d' % sys.version_info[:2])"
} catch {
    Write-Host "python not found on PATH." -ForegroundColor Red
    exit 1
}
Write-Host "  python $version"

$parts = $version.Split(".")
if ([int]$parts[0] -lt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -lt 9)) {
    Write-Host "  Python 3.9 or newer is required (ctranslate2 ships no older wheels)." -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------------------
Write-Step "2/6  Virtual environment"

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

if (Test-Path $venvPython) {
    Write-Host "  .venv already exists, reusing it"
} else {
    Write-Host "  creating .venv"
    & $python -m venv .venv
    Assert-LastExit "python -m venv .venv"
}

& $venvPython -m pip install --upgrade pip --quiet
Assert-LastExit "pip upgrade"

# ---------------------------------------------------------------------------
Write-Step "3/6  Dependencies"

& $venvPython -m pip install -r requirements.txt
Assert-LastExit "pip install -r requirements.txt"

# ---------------------------------------------------------------------------
Write-Step "4/6  CUDA libraries"

Write-Host "  CTranslate2 needs cuBLAS and cuDNN at load time. On Windows nothing"
Write-Host "  puts them on the search path for you, so we try the cheap option first."
Write-Host ""

if ($WithTorchCuda) {
    Write-Host "  -WithTorchCuda: installing the PyTorch CUDA build (~2.5 GB)."
    Write-Host "  It is never imported; only its bundled DLLs in torch/lib are used."
    & $venvPython -m pip install torch --index-url https://download.pytorch.org/whl/cu124
    Assert-LastExit "pip install torch (cu124)"
} else {
    Write-Host "  trying the NVIDIA redistributable wheels..."
    & $venvPython -m pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "  Those wheels are not installable here (they are not always published" -ForegroundColor Yellow
        Write-Host "  for win_amd64). Not fatal yet: the DLLs may already be present from a" -ForegroundColor Yellow
        Write-Host "  CUDA Toolkit install. The check in step 5 decides." -ForegroundColor Yellow
        $global:LASTEXITCODE = 0
    }
}

# ---------------------------------------------------------------------------
Write-Step "5/6  Does CTranslate2 see the GPU?"

& $venvPython scripts\verify_cuda.py
$cudaOk = ($LASTEXITCODE -eq 0)

if (-not $cudaOk) {
    if ($IgnoreCudaCheck) {
        Write-Host ""
        Write-Host "  -IgnoreCudaCheck given, carrying on anyway." -ForegroundColor Yellow
    } else {
        Write-Host ""
        Write-Host ("-" * 72) -ForegroundColor Yellow
        Write-Host "Stopping BEFORE the 3.5 GB download, on purpose." -ForegroundColor Yellow
        Write-Host ""
        Write-Host "  Most likely fix on this machine:" -ForegroundColor Yellow
        Write-Host "      .\setup.ps1 -WithTorchCuda" -ForegroundColor Yellow
        Write-Host ""
        Write-Host "  Or, to download the model anyway and sort CUDA out later:" -ForegroundColor Yellow
        Write-Host "      .\setup.ps1 -IgnoreCudaCheck" -ForegroundColor Yellow
        Write-Host ("-" * 72) -ForegroundColor Yellow
        exit 1
    }
}

# ---------------------------------------------------------------------------
Write-Step "6/6  Model"

if ($SkipModel) {
    Write-Host "  -SkipModel given, skipping the download."
} else {
    Write-Host "  destination: $ModelDir"
    & $venvPython scripts\download_model.py --dest "$ModelDir"
    Assert-LastExit "download_model.py"
}

# ---------------------------------------------------------------------------
Write-Step "Done"

Write-Host "  Start the service:      .\run.ps1"
Write-Host "  Then check it:          curl http://127.0.0.1:8100/health"
Write-Host ""
if (-not $cudaOk) {
    Write-Host "  Reminder: the CUDA check did not pass. The service will refuse to load" -ForegroundColor Yellow
    Write-Host "  on the GPU. Re-run scripts\verify_cuda.py after fixing it." -ForegroundColor Yellow
}
