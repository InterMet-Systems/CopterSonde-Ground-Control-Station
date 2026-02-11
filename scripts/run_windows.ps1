# CopterSonde GCS – Windows launcher (PowerShell)
# Run from the repo root:  .\scripts\run_windows.ps1

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot

Push-Location $repoRoot
try {
    if (-not (Test-Path ".venv\Scripts\Activate.ps1")) {
        Write-Host "Creating virtual environment …"
        python -m venv .venv
        & .venv\Scripts\Activate.ps1
        pip install -r requirements.txt
    } else {
        & .venv\Scripts\Activate.ps1
    }

    python app\main.py
} finally {
    Pop-Location
}
