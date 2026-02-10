[CmdletBinding()]
param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path,
    [int]$Port = 8009
)

$ErrorActionPreference = "Stop"
Set-Location $RepoRoot

$venvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    throw "Virtual environment python not found: $venvPython"
}

$env:PYTHONUNBUFFERED = "1"
& $venvPython "manage.py" "runserver" "0.0.0.0:$Port"
