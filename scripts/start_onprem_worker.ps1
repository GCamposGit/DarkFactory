<#
.SYNOPSIS
    Starts or manages a DarkFac fleet/on-premises production-line worker (USR-286 / HF-27-09).

.DESCRIPTION
    Launches `cloud_worker` pointed at the shared Postgres control store over Tailscale,
    with capabilities, priority, and capacity appropriate for this fleet host.
    Enforces the distributed fleet topology:
    - 1 cycle per host (MaxSlots: 1)
    - Grokbox (priority: primary / 1) > Notebook (secondary / 2) > Desktop (fallback / 3)
    - Non-pinned model/harness routing via core.line.routing.pick
    - Supports -Drain, -Status, -Probe, -Rollback actions.

.PARAMETER DatabaseUrl
    Postgres connection string reachable over Tailscale (DARKFAC_HF02_DATABASE_URL).

.PARAMETER HostId
    Logical fleet host identifier: "grokbox", "notebook", "desktop" (autodetected if omitted).

.PARAMETER WorkerId
    Identifier for this worker (default: fleet-<HostId>).

.PARAMETER Caps
    Comma-separated capability list to publish. Autodetected when omitted.

.PARAMETER Priority
    One of "primary", "secondary", "fallback" (DARKFAC_WORKER_PRIORITY).
    Auto-derived from HostId if omitted (grokbox: primary, notebook: secondary, desktop: fallback).

.PARAMETER MaxSlots
    Maximum concurrent demand cycles for this host (default: 1, per fleet topology).

.PARAMETER RootPath
    Explicit path to repository root. Discovered automatically if omitted.

.PARAMETER Headless
    Launch detached in the background as a daemon.

.PARAMETER Drain
    Signal running worker to drain (finish current in-flight cycle, reject new admissions).

.PARAMETER Status
    Query status of the local fleet worker daemon.

.PARAMETER Probe
    Probe inventory and capabilities of this host.

.PARAMETER Rollback
    Suspend admission for this host, preserving worktrees and evidence.
#>

[CmdletBinding()]
param (
    [string]$DatabaseUrl = "",
    [ValidateSet("grokbox", "notebook", "desktop", "")]
    [string]$HostId = "",
    [string]$WorkerId = "",
    [string]$Caps = "",
    [ValidateSet("primary", "secondary", "fallback")]
    [string]$Priority = "secondary",
    [int]$MaxSlots = 1,
    [string]$RootPath = "",
    [switch]$Headless,
    [switch]$Drain,
    [switch]$Status,
    [switch]$Probe,
    [switch]$Rollback
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

# 3. Resolve HostId and Priority
$resolvedHostId = $HostId
if ([string]::IsNullOrWhiteSpace($resolvedHostId)) {
    $cName = $env:COMPUTERNAME.ToLowerInvariant()
    if ($cName -match "grok") {
        $resolvedHostId = "grokbox"
    } elseif ($cName -match "desktop" -or $cName -match "g45ipem") {
        $resolvedHostId = "desktop"
    } else {
        $resolvedHostId = "notebook"
    }
}

$resolvedPriority = $Priority
if (-not $PSBoundParameters.ContainsKey('Priority')) {
    switch ($resolvedHostId) {
        "grokbox" { $resolvedPriority = "primary" }
        "notebook" { $resolvedPriority = "secondary" }
        "desktop" { $resolvedPriority = "fallback" }
        default { $resolvedPriority = "secondary" }
    }
}

# 4. Handle Subcommand Switches via start_fleet_worker.py
$fleetScript = Join-Path $candidateRoot "scripts\start_fleet_worker.py"
if ($Probe) {
    & python $fleetScript probe --host $resolvedHostId
    exit $LASTEXITCODE
}
if ($Status) {
    & python $fleetScript status --host $resolvedHostId
    exit $LASTEXITCODE
}
if ($Drain) {
    & python $fleetScript drain --host $resolvedHostId
    exit $LASTEXITCODE
}
if ($Rollback) {
    & python $fleetScript rollback --host $resolvedHostId
    exit $LASTEXITCODE
}

# 5. Resolve Postgres URL
$resolvedDatabaseUrl = $DatabaseUrl
if ([string]::IsNullOrWhiteSpace($resolvedDatabaseUrl)) {
    $resolvedDatabaseUrl = $env:DARKFAC_HF02_DATABASE_URL
}
if ([string]::IsNullOrWhiteSpace($resolvedDatabaseUrl)) {
    Write-Error "[WORKER_START_FAIL] No Postgres URL. Pass -DatabaseUrl or set DARKFAC_HF02_DATABASE_URL."
    exit 1
}

# 6. Resolve worker id
$resolvedWorkerId = $WorkerId
if ([string]::IsNullOrWhiteSpace($resolvedWorkerId)) {
    $resolvedWorkerId = "fleet-$resolvedHostId"
}

# 7. Autodetect capabilities when not given explicitly
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
    $detected.Add("target:local_service")
    $resolvedCaps = [string]::Join(",", $detected)
}

Write-Output "=================================================================="
Write-Output " [DarkFac Distributed Fleet Worker Launcher (USR-286 / F7)]"
Write-Output " Host ID      : $resolvedHostId"
Write-Output " Worker ID    : $resolvedWorkerId"
Write-Output " Priority     : $resolvedPriority"
Write-Output " Max Slots    : $MaxSlots"
Write-Output " Capabilities : $resolvedCaps"
Write-Output " Project Root : $candidateRoot"
Write-Output "=================================================================="

$env:DARKFAC_HF02_DATABASE_URL = $resolvedDatabaseUrl
$env:DARKFAC_HOST_ID = $resolvedHostId
$env:DARKFAC_WORKER_ID = $resolvedWorkerId
$env:DARKFAC_WORKER_CAPS = $resolvedCaps
$env:DARKFAC_WORKER_PRIORITY = $resolvedPriority
$env:DARKFAC_MAX_CONCURRENT_SLOTS = "$MaxSlots"

# 8. Dispatch through start_fleet_worker.py or directly
if ($Headless) {
    & python $fleetScript start --host $resolvedHostId --headless --db-url $resolvedDatabaseUrl
    exit $LASTEXITCODE
}

# 9. Foreground launch with unbuffered output
& python -u -m core.orchestrator.cloud_worker
