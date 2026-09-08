<#
.SYNOPSIS
    Stops the DarkFac On-Premises Remote Test Worker Daemon.

.DESCRIPTION
    Terminates the background worker daemon by PID file or by listening port (8080).
#>

[CmdletBinding()]
param (
    [int]$Port = 8080,
    [string]$RootPath = ""
)

$ErrorActionPreference = "SilentlyContinue"

# 1. Discover Project Root
if ([string]::IsNullOrWhiteSpace($RootPath)) {
    $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
    $candidateRoot = Split-Path -Parent $scriptDir
} else {
    $candidateRoot = (Resolve-Path -LiteralPath $RootPath).Path
}

$pidFile = Join-Path $candidateRoot ".factory\pids\onprem_worker.pid"
$stopped = $false

# Try stopping via PID file
if (Test-Path -LiteralPath $pidFile) {
    $savedPid = Get-Content -Path $pidFile -Raw
    if (-not [string]::IsNullOrWhiteSpace($savedPid)) {
        $pidInt = [int]($savedPid.Trim())
        Write-Output "[WORKER_STOP] Stopping worker by PID $pidInt..."
        Stop-Process -Id $pidInt -Force -ErrorAction SilentlyContinue
        Remove-Item -Path $pidFile -Force -ErrorAction SilentlyContinue
        $stopped = $true
    }
}

# Also ensure any process listening on port is terminated
$connections = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
foreach ($conn in $connections) {
    if ($conn.OwningProcess -gt 0) {
        Write-Output "[WORKER_STOP] Stopping process on port $Port (PID: $($conn.OwningProcess))..."
        Stop-Process -Id $conn.OwningProcess -Force -ErrorAction SilentlyContinue
        $stopped = $true
    }
}

if ($stopped) {
    Write-Output "[WORKER_STOP_OK] Worker daemon stopped successfully."
} else {
    Write-Output "[WORKER_STOP_INFO] No running worker daemon found on port $Port."
}
