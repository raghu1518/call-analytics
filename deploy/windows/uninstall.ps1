[CmdletBinding()]
param(
    [bool]$StopProcesses = $true,
    [switch]$PurgeVenv,
    [switch]$PurgeData,
    [switch]$PurgeLogs,
    [switch]$PurgeEnv
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

$TaskNames = @(
    "CallAnalytics-Web",
    "CallAnalytics-GenesysConnector",
    "CallAnalytics-GenesysAudioHook"
)

function Write-Step {
    param([string]$Message)
    Write-Host "[deploy/windows] $Message"
}

Write-Step "Repository directory: $RepoRoot"

foreach ($taskName in $TaskNames) {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($null -ne $task) {
        Write-Step "Removing task: $taskName"
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }
}

if ($StopProcesses) {
    Write-Step "Stopping running repo Python processes"
    $escapedRepo = [regex]::Escape($RepoRoot)
    $procs = Get-CimInstance Win32_Process | Where-Object {
        $_.Name -match "^python(\.exe)?$" -and
        $_.CommandLine -match "manage\.py" -and
        $_.CommandLine -match $escapedRepo
    }
    foreach ($proc in $procs) {
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

if ($PurgeVenv) {
    $venv = Join-Path $RepoRoot ".venv"
    if (Test-Path $venv) {
        Write-Step "Removing virtual environment"
        Remove-Item -Path $venv -Recurse -Force
    }
}

if ($PurgeData) {
    $dataDir = Join-Path $RepoRoot "data"
    if (Test-Path $dataDir) {
        Write-Step "Removing data directory"
        Remove-Item -Path $dataDir -Recurse -Force
    }
}

if ($PurgeLogs) {
    $logDir = Join-Path $RepoRoot "log"
    if (Test-Path $logDir) {
        Write-Step "Removing log directory"
        Remove-Item -Path $logDir -Recurse -Force
    }
}

if ($PurgeEnv) {
    $envFile = Join-Path $RepoRoot ".env"
    if (Test-Path $envFile) {
        Write-Step "Removing .env"
        Remove-Item -Path $envFile -Force
    }
}

Write-Step "Windows uninstall completed."
