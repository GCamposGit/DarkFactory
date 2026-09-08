<#
.SYNOPSIS
    Installs automatic, invisible startup for the DarkFac Remote Test Worker (No Admin Rights Needed).

.DESCRIPTION
    Creates a silent launcher script in the current user's Windows Startup folder:
    %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\DarkFacWorker.vbs
    When Windows starts or the user logs in, the VBScript silently launches the worker daemon
    with -Headless in the background, without any console window or administrator prompt.
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

$launcher = Join-Path $candidateRoot "scripts\start_onprem_worker.ps1"
if (-not (Test-Path -LiteralPath $launcher)) {
    Write-Error "[STARTUP_FAIL] Cannot find launcher at '$launcher'"
    exit 1
}

$startupFolder = [System.Environment]::GetFolderPath([System.Environment+SpecialFolder]::Startup)
$vbsPath = Join-Path $startupFolder "DarkFacWorker.vbs"

$vbsContent = @"
Set WshShell = CreateObject("WScript.Shell")
WshShell.Run "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$launcher`" -Headless", 0, False
"@

Set-Content -Path $vbsPath -Value $vbsContent -Encoding Ascii -Force

Write-Output "=================================================================="
Write-Output " [DarkFac User Startup Installed (Zero-Admin)]"
Write-Output " Startup File : $vbsPath"
Write-Output " Launcher     : $launcher"
Write-Output " Trigger      : User Logon / Windows Boot (100% Silent VBScript)"
Write-Output " Status       : Active & Permanent"
Write-Output "=================================================================="
