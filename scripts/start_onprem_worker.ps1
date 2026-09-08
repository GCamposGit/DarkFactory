<#
.SYNOPSIS
    Starts the DarkFac On-Premises Remote Test Worker Daemon.

.DESCRIPTION
    Launches the FastAPI headless test worker daemon on the designated machine
    (desktop-g45ipem, Tailscale 100.78.181.90, or any on-premises worker node).
    Guards execution with -NoProfile -NonInteractive -ExecutionPolicy Bypass and anchors CWD.

.PARAMETER HostAddress
    Address to bind (default: 0.0.0.0).

.PARAMETER Port
    Port to listen on (default: 8080).

.PARAMETER NodeId
    Identifier for the node (default: onprem-z97-server).

.PARAMETER RootPath
    Explicit path to repository root. Discovered automatically if omitted.
#>

[CmdletBinding()]
param (
    [string]$HostAddress = "0.0.0.0",
    [int]$Port = 8080,
    [string]$NodeId = "onprem-z97-server",
    [string]$RootPath = "",
    [switch]$Headless
)

$ErrorActionPreference = "Stop"

# 1. Discover Project Root
if ([string]::IsNullOrWhiteSpace($RootPath)) {
    $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
    $candidateRoot = Split-Path -Parent $scriptDir
} else {
    $candidateRoot = (Resolve-Path -LiteralPath $RootPath).Path
}

# 2. Run Terminal Init Guard if available
$initScript = Join-Path $candidateRoot "scripts\init_terminal.ps1"
if (Test-Path -LiteralPath $initScript) {
    & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $initScript -Quiet
}

Set-Location -LiteralPath $candidateRoot
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

$workerScript = Join-Path $candidateRoot "core\harness\remote_worker.py"
if (-not (Test-Path -LiteralPath $workerScript)) {
    Write-Error "[WORKER_START_FAIL] Cannot find worker script at '$workerScript'"
    exit 1
}

# 3. Headless background launch if requested
if ($Headless) {
    $logDir = Join-Path $candidateRoot ".factory\test_logs"
    if (-not (Test-Path -LiteralPath $logDir)) {
        New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    }
    $daemonLog = Join-Path $logDir "remote_worker_daemon.log"
    $pidDir = Join-Path $candidateRoot ".factory\pids"
    if (-not (Test-Path -LiteralPath $pidDir)) {
        New-Item -ItemType Directory -Force -Path $pidDir | Out-Null
    }
    $pidFile = Join-Path $pidDir "onprem_worker.pid"

    Write-Output "[WORKER_HEADLESS] Launching remote test worker in the background (WindowStyle: Hidden)..."
    $argList = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command `"python -u `'$workerScript`' --host $HostAddress --port $Port --node-id $NodeId --project-root `'$candidateRoot`' *>> `'$daemonLog`'`""
    $proc = Start-Process -FilePath "powershell.exe" -ArgumentList $argList -WindowStyle Hidden -PassThru

    $proc.Id | Out-File -FilePath $pidFile -Encoding utf8 -Force
    Write-Output "[WORKER_HEADLESS_OK] Worker daemon running in background (PID: $($proc.Id))"
    Write-Output "  Endpoint: http://${HostAddress}:${Port}"
    Write-Output "  Log file: $daemonLog"
    Write-Output "  PID file: $pidFile"
    exit 0
}

Write-Output "=================================================================="
Write-Output " [DarkFac On-Premises Test Worker Launcher]"
Write-Output " Node ID     : $NodeId"
Write-Output " Project Root: $candidateRoot"
Write-Output " Endpoint    : http://${HostAddress}:${Port}"
Write-Output " Health Check: http://${HostAddress}:${Port}/health"
Write-Output " Python Exec : $(Get-Command python | Select-Object -ExpandProperty Source)"
Write-Output "=================================================================="

# 4. Launch the daemon with unbuffered output
& python -u $workerScript --host $HostAddress --port $Port --node-id $NodeId --project-root $candidateRoot
