<#
.SYNOPSIS
    Installs automatic, invisible startup for the DarkFac Remote Test Worker (Port 8080).

.DESCRIPTION
    Creates a silent launcher script in the current user's Windows Startup folder:
    %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\DarkFacTestWorker.vbs
    When Windows boots or the user logs in, the VBScript silently launches the test worker daemon
    in the background without any console window or administrator prompt (Zero-Admin).
#>

[CmdletBinding()]
param (
    [string]$RootPath = "",
    [int]$Port = 8080
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($RootPath)) {
    $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
    $candidateRoot = Split-Path -Parent $scriptDir
} else {
    $candidateRoot = (Resolve-Path -LiteralPath $RootPath).Path
}

$workerScript = Join-Path $candidateRoot "core\harness\remote_worker.py"
if (-not (Test-Path -LiteralPath $workerScript)) {
    Write-Error "[STARTUP_FAIL] Cannot find test worker script at '$workerScript'"
    exit 1
}

$startupFolder = [System.Environment]::GetFolderPath([System.Environment+SpecialFolder]::Startup)
$vbsPath = Join-Path $startupFolder "DarkFacTestWorker.vbs"

$vbsContent = @"
Set WshShell = CreateObject("WScript.Shell")
WshShell.Run "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command ""`$env:PYTHONIOENCODING='utf-8'; `$env:PYTHONUTF8='1'; python '$workerScript' --host 0.0.0.0 --port $Port""", 0, False
"@

Set-Content -Path $vbsPath -Value $vbsContent -Encoding Ascii -Force

Write-Output "=================================================================="
Write-Output " [DarkFac Test Worker Daemon (Port $Port) Startup Installed]"
Write-Output " Startup File : $vbsPath"
Write-Output " Worker Script: $workerScript"
Write-Output " Trigger      : User Logon / Windows Boot (100% Silent VBScript)"
Write-Output " Status       : Active & Permanent"
Write-Output "=================================================================="
