[CmdletBinding()]
param(
    [int]$WebPort = 8009,
    [string]$PythonCommand = "",
    [switch]$SkipScheduledTasks,
    [switch]$SkipConnectorTask,
    [switch]$SkipAudioHookTask
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$CurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

$TaskNameWeb = "CallAnalytics-Web"
$TaskNameConnector = "CallAnalytics-GenesysConnector"
$TaskNameAudioHook = "CallAnalytics-GenesysAudioHook"

function Write-Step {
    param([string]$Message)
    Write-Host "[deploy/windows] $Message"
}

function Resolve-PythonLauncher {
    param([string]$Override)
    if ($Override) {
        if (-not (Get-Command $Override -ErrorAction SilentlyContinue)) {
            throw "Python command not found: $Override"
        }
        return $Override
    }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        return "py"
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        return "python"
    }
    throw "Python launcher not found. Install Python 3.10+ and retry."
}

function Invoke-VenvCreate {
    param(
        [string]$Launcher,
        [string]$TargetPath
    )
    if (Test-Path $TargetPath) {
        return
    }
    if ($Launcher -eq "py") {
        & py -3 -m venv $TargetPath
    } else {
        & $Launcher -m venv $TargetPath
    }
}

function Ensure-ScheduledTask {
    param(
        [string]$TaskName,
        [string]$ScriptPath,
        [string]$ExtraArguments
    )
    $resolvedScript = (Resolve-Path $ScriptPath).Path
    $taskArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$resolvedScript`" -RepoRoot `"$RepoRoot`" $ExtraArguments"
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $taskArgs
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $principal = New-ScheduledTaskPrincipal -UserId $CurrentUser -LogonType Interactive -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -StartWhenAvailable

    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }

    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
    Write-Step "Registered task: $TaskName"
    try {
        Start-ScheduledTask -TaskName $TaskName
    } catch {
        Write-Warning "Task registered but could not be started immediately: $TaskName"
    }
}

Write-Step "Repository directory: $RepoRoot"

$venvPath = Join-Path $RepoRoot ".venv"
$pythonLauncher = Resolve-PythonLauncher -Override $PythonCommand

Write-Step "Creating/updating Python virtual environment"
Invoke-VenvCreate -Launcher $pythonLauncher -TargetPath $venvPath

$venvPython = Join-Path $venvPath "Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    throw "Virtual environment python missing: $venvPython"
}

Write-Step "Installing Python dependencies"
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r (Join-Path $RepoRoot "requirements.txt")

$envFile = Join-Path $RepoRoot ".env"
$envExample = Join-Path $RepoRoot ".env.example"
if (-not (Test-Path $envFile) -and (Test-Path $envExample)) {
    Write-Step "Creating .env from .env.example"
    Copy-Item $envExample $envFile
} else {
    Write-Step ".env already exists, keeping current values"
}

Write-Step "Creating required runtime directories"
$dirs = @(
    "data\uploads",
    "data\outputs",
    "data\db",
    "data\runtime\live_audio",
    "log"
)
foreach ($dir in $dirs) {
    $full = Join-Path $RepoRoot $dir
    if (-not (Test-Path $full)) {
        New-Item -ItemType Directory -Path $full -Force | Out-Null
    }
}

Write-Step "Running migrations and Django checks"
& $venvPython (Join-Path $RepoRoot "manage.py") migrate
& $venvPython (Join-Path $RepoRoot "manage.py") check

if (-not $SkipScheduledTasks) {
    Write-Step "Registering scheduled tasks"
    Ensure-ScheduledTask -TaskName $TaskNameWeb -ScriptPath (Join-Path $RepoRoot "deploy\windows\start-web.ps1") -ExtraArguments "-Port $WebPort"
    if (-not $SkipConnectorTask) {
        Ensure-ScheduledTask -TaskName $TaskNameConnector -ScriptPath (Join-Path $RepoRoot "deploy\windows\start-genesys-connector.ps1") -ExtraArguments ""
    }
    if (-not $SkipAudioHookTask) {
        Ensure-ScheduledTask -TaskName $TaskNameAudioHook -ScriptPath (Join-Path $RepoRoot "deploy\windows\start-audiohook.ps1") -ExtraArguments ""
    }
} else {
    Write-Step "Skipping scheduled task setup"
}

Write-Host ""
Write-Host "Windows deployment completed."
Write-Host "Repo: $RepoRoot"
Write-Host "Web URL: http://127.0.0.1:$WebPort"
Write-Host "Task names: $TaskNameWeb, $TaskNameConnector, $TaskNameAudioHook"
