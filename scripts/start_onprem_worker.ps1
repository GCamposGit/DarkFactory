<#
.SYNOPSIS
    Starts a DarkFac on-premises production-line worker (core.orchestrator.cloud_worker).

.DESCRIPTION
    HF-27-09: launches the same `cloud_worker` the VPS runs, pointed at the
    shared Postgres control store over Tailscale, with capabilities and
    priority appropriate for this host (Desktop or Notebook). There is no
    synchronous HTTP push from the cloud to on-prem hosts: this worker polls
    the queue and claims jobs whose `required_capabilities` it can satisfy
    (see docs/PRODUCTION_LINE_PLAN_2026-09-22.md section 3 and
    docs/handoffs/production-line/HF-27-09.md).

    Capabilities are autodetected from what is actually installed on PATH
    unless -Caps is given explicitly. Autodetection never fails the script:
    an undetectable tool is simply left out of the published capability set.

.PARAMETER DatabaseUrl
    Postgres connection string reachable over the Tailscale tailnet
    (DARKFAC_HF02_DATABASE_URL). Falls back to the existing
    $env:DARKFAC_HF02_DATABASE_URL if already set on the host (e.g. via the
    Windows user/machine environment), so scheduled-task relaunches do not
    need to pass it again. The script exits with a clear error if neither
    is available.

.PARAMETER WorkerId
    Identifier for this worker (default: derived from the machine name).

.PARAMETER Caps
    Comma-separated capability list to publish (e.g.
    "git,gh,node,python,harness:claude,harness:grok,target:local_service").
    Autodetected when omitted.

.PARAMETER Priority
    One of "primary", "secondary", "fallback" (DARKFAC_WORKER_PRIORITY).
    Controls poll interval and the ready_age claim filter in
    core.orchestrator.cloud_worker. Default: "secondary" (Desktop).
    Use "fallback" for the Notebook.

.PARAMETER MaxSlots
    DARKFAC_MAX_CONCURRENT_SLOTS for this host (default: 3, per the plan's
    Desktop sizing; pass 1 or 2 for the Notebook).

.PARAMETER RootPath
    Explicit path to repository root. Discovered automatically if omitted.

.PARAMETER Headless
    Launch detached in the background (used by the scheduled-task /
    startup-folder installers) instead of blocking the current console.
#>

[CmdletBinding()]
param (
    [string]$DatabaseUrl = "",
    [string]$WorkerId = "",
    [string]$Caps = "",
    [ValidateSet("primary", "secondary", "fallback")]
    [string]$Priority = "secondary",
    [int]$MaxSlots = 3,
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
$env:PYTHONPATH = $candidateRoot

# 3. Resolve the Postgres URL: explicit param wins, else whatever the host
#    environment already has (set once via the Tailscale-reachable
#    connection string per docs/runbooks/HF-27-09_topology.md).
$resolvedDatabaseUrl = $DatabaseUrl
if ([string]::IsNullOrWhiteSpace($resolvedDatabaseUrl)) {
    $resolvedDatabaseUrl = $env:DARKFAC_HF02_DATABASE_URL
}
if ([string]::IsNullOrWhiteSpace($resolvedDatabaseUrl)) {
    Write-Error "[WORKER_START_FAIL] No Postgres URL. Pass -DatabaseUrl or set DARKFAC_HF02_DATABASE_URL (Tailscale tailnet address; see docs/runbooks/HF-27-09_topology.md)."
    exit 1
}

# 4. Resolve worker id
$resolvedWorkerId = $WorkerId
if ([string]::IsNullOrWhiteSpace($resolvedWorkerId)) {
    $resolvedWorkerId = "onprem-$($env:COMPUTERNAME)".ToLowerInvariant()
}

# 5. Autodetect capabilities when not given explicitly.
function Test-CommandAvailable {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

$resolvedCaps = $Caps
if ([string]::IsNullOrWhiteSpace($resolvedCaps)) {
    $detected = New-Object System.Collections.Generic.List[string]
    if (Test-CommandAvailable "git") { $detected.Add("git") }
    if (Test-CommandAvailable "gh") { $detected.Add("gh") }
    if (Test-CommandAvailable "node") { $detected.Add("node") }
    if (Test-CommandAvailable "python") { $detected.Add("python") }
    if (Test-CommandAvailable "claude") { $detected.Add("harness:claude") }
    if (Test-CommandAvailable "codex") { $detected.Add("harness:codex") }
    if (Test-CommandAvailable "grok") { $detected.Add("harness:grok") }
    if (Test-CommandAvailable "antigravity") { $detected.Add("harness:antigravity") }
    if (Test-CommandAvailable "nvidia-smi") { $detected.Add("gpu") }
    # This host runs off-VPS Python targets directly (no container isolation).
    $detected.Add("target:local_service")
    $resolvedCaps = [string]::Join(",", $detected)
}

Write-Output "=================================================================="
Write-Output " [DarkFac On-Premises Worker Launcher (HF-27-09)]"
Write-Output " Worker ID    : $resolvedWorkerId"
Write-Output " Priority     : $Priority"
Write-Output " Max Slots    : $MaxSlots"
Write-Output " Capabilities : $resolvedCaps"
Write-Output " Project Root : $candidateRoot"
Write-Output "=================================================================="

$env:DARKFAC_HF02_DATABASE_URL = $resolvedDatabaseUrl
$env:DARKFAC_WORKER_ID = $resolvedWorkerId
$env:DARKFAC_WORKER_CAPS = $resolvedCaps
$env:DARKFAC_WORKER_PRIORITY = $Priority
$env:DARKFAC_MAX_CONCURRENT_SLOTS = "$MaxSlots"

$workerModuleCheck = Join-Path $candidateRoot "core\orchestrator\cloud_worker.py"
if (-not (Test-Path -LiteralPath $workerModuleCheck)) {
    Write-Error "[WORKER_START_FAIL] Cannot find worker module at '$workerModuleCheck'"
    exit 1
}

# 6. Headless background launch if requested (scheduled task / startup folder)
if ($Headless) {
    $logDir = Join-Path $candidateRoot ".factory\test_logs"
    if (-not (Test-Path -LiteralPath $logDir)) {
        New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    }
    $daemonLog = Join-Path $logDir "onprem_cloud_worker_daemon.log"
    $pidDir = Join-Path $candidateRoot ".factory\pids"
    if (-not (Test-Path -LiteralPath $pidDir)) {
        New-Item -ItemType Directory -Force -Path $pidDir | Out-Null
    }
    $pidFile = Join-Path $pidDir "onprem_worker.pid"

    Write-Output "[WORKER_HEADLESS] Launching on-prem cloud_worker in the background (WindowStyle: Hidden)..."
    $argList = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command `"" +
        "`$env:PYTHONIOENCODING='utf-8'; `$env:PYTHONUTF8='1'; `$env:PYTHONPATH='$candidateRoot'; " +
        "`$env:DARKFAC_HF02_DATABASE_URL='$resolvedDatabaseUrl'; `$env:DARKFAC_WORKER_ID='$resolvedWorkerId'; " +
        "`$env:DARKFAC_WORKER_CAPS='$resolvedCaps'; `$env:DARKFAC_WORKER_PRIORITY='$Priority'; " +
        "`$env:DARKFAC_MAX_CONCURRENT_SLOTS='$MaxSlots'; " +
        "python -u -m core.orchestrator.cloud_worker *>> `'$daemonLog`'`""
    $proc = Start-Process -FilePath "powershell.exe" -WorkingDirectory $candidateRoot -ArgumentList $argList -WindowStyle Hidden -PassThru

    $proc.Id | Out-File -FilePath $pidFile -Encoding utf8 -Force
    Write-Output "[WORKER_HEADLESS_OK] Worker daemon running in background (PID: $($proc.Id))"
    Write-Output "  Log file: $daemonLog"
    Write-Output "  PID file: $pidFile"
    exit 0
}

# 7. Foreground launch with unbuffered output
& python -u -m core.orchestrator.cloud_worker
