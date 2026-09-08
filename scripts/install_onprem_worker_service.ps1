<#
.SYNOPSIS
    Registers the DarkFac Remote Test Worker as an automatic, invisible Windows Scheduled Task.

.DESCRIPTION
    Creates or updates the scheduled task "DarkFac-OnPrem-Worker" that starts
    the worker daemon in the background (-Headless) on user logon or system boot,
    ensuring it runs 24/7 without opening any console windows.

.PARAMETER RootPath
    Explicit path to repository root (defaults to discover).
#>

[CmdletBinding()]
param (
    [string]$RootPath = ""
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($RootPath)) {
    $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
    $candidateRoot = Split-Path -Parent $scriptDir
} else {
    $candidateRoot = (Resolve-Path -LiteralPath $RootPath).Path
}

$taskName = "DarkFac-OnPrem-Worker"
$launcher = Join-Path $candidateRoot "scripts\start_onprem_worker.ps1"

if (-not (Test-Path -LiteralPath $launcher)) {
    Write-Error "[TASK_INSTALL_FAIL] Cannot find launcher at '$launcher'"
    exit 1
}

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$launcher`" -Headless" `
    -WorkingDirectory $candidateRoot

$trigger = New-ScheduledTaskTrigger -AtLogOn

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Days 365) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -Hidden

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Dark Factory On-Premises Headless Test Worker Daemon (Port 8080)" `
    -Force | Out-Null

Write-Output "=================================================================="
Write-Output " [DarkFac Scheduled Task Registered]"
Write-Output " Task Name    : $taskName"
Write-Output " Trigger      : At Logon (Silent / Hidden)"
Write-Output " Working Dir  : $candidateRoot"
Write-Output " Script       : $launcher"
Write-Output " Status       : Active & Permanent"
Write-Output "=================================================================="
