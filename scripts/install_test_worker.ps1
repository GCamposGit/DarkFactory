<#
.SYNOPSIS
    Idempotent installer for the DarkFac primary test worker daemon
    (core/harness/remote_worker.py) as an always-on Windows Scheduled Task.

.DESCRIPTION
    HF-27-11: the Desktop (darkfac-desktop, Tailscale 100.78.181.90) is the
    owner-decided ALWAYS-primary test worker for the validation harness's
    remote dispatch (core/harness/remote_dispatch.py), so the Notebook stays
    free for interactive dev. This script:

      1. Verifies Python 3.12+ and git are on PATH.
      2. Ensures the repo exists at -RepoPath (clones it if missing, else
         `git pull`).
      3. Installs/updates Python dependencies (`pip install -r
         requirements.txt`).
      4. Registers (or replaces) a Scheduled Task "DarkFac Test Worker" that
         runs `python core/harness/remote_worker.py --host 0.0.0.0 --port
         8080 --node-id darkfac-desktop` at logon AND at system startup,
         hidden (no console window), auto-restarting on failure.
      5. Adds an inbound firewall rule for TCP 8080 restricted to the
         Tailscale CGNAT range (100.64.0.0/10) -- the only step that needs
         Administrator; everything else runs as a normal user.
      6. Optionally sets DARKFAC_WORKER_TOKEN (user + machine environment)
         if -Token is passed, for optional bearer-token auth on the
         worker's mutating endpoints.
      7. Starts the task immediately and verifies
         http://127.0.0.1:8080/health responds.

    Safe to re-run: every step either checks-then-skips or replaces its own
    prior state (no duplicate tasks/rules/env entries).

.PARAMETER RepoPath
    Where the DarkFac repo lives (or should be cloned to). Default:
    C:\dev\DarkFac.

.PARAMETER Token
    Optional bearer token to set as DARKFAC_WORKER_TOKEN (user + machine
    environment) for the worker's optional auth. Omit to leave the worker
    open to anything that can reach it on the tailnet (still restricted by
    the firewall rule to 100.64.0.0/10).

.PARAMETER Port
    TCP port for the worker daemon and its firewall rule. Default: 8080.

.EXAMPLE
    # Normal use (from an elevated PowerShell window so the firewall rule
    # step succeeds in the same run):
    .\install_test_worker.ps1

.EXAMPLE
    # With an auth token, non-default repo path:
    .\install_test_worker.ps1 -RepoPath D:\DarkFac -Token "a-long-random-string"
#>

[CmdletBinding()]
param (
    [string]$RepoPath = "C:\dev\DarkFac",
    [string]$Token = "",
    [int]$Port = 8080
)

$ErrorActionPreference = "Stop"
$TaskName = "DarkFac Test Worker"
$NodeId = "darkfac-desktop"
$OriginUrl = "https://github.com/GCamposGit/DarkFactory.git"
$TailscaleCidr = "100.64.0.0/10"

function Write-Section {
    param([string]$Text)
    Write-Output ""
    Write-Output "== $Text =="
}

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

$overallOk = $true

Write-Output "======================================================================"
Write-Output " DarkFac Primary Test Worker Installer (HF-27-11)"
Write-Output " Repo path : $RepoPath"
Write-Output " Node id   : $NodeId"
Write-Output " Port      : $Port"
Write-Output "======================================================================"

# --- 1. Verify Python 3.12+ and git -----------------------------------------

Write-Section "Step 1/7: checking prerequisites (Python 3.12+, git)"

$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
    Write-Error "[FAIL] python was not found on PATH. Install Python 3.12+ from https://www.python.org/downloads/windows/ (check 'Add python.exe to PATH' during setup), then re-run this script."
    exit 1
}
$pythonVersionRaw = (& python --version) 2>&1
Write-Output "[OK] Found: $pythonVersionRaw ($($pythonCmd.Source))"
if ($pythonVersionRaw -notmatch "Python 3\.(1[2-9]|[2-9][0-9])") {
    Write-Warning "[WARN] Expected Python 3.12+; found '$pythonVersionRaw'. Continuing, but core/harness relies on 3.12+ syntax."
}

$gitCmd = Get-Command git -ErrorAction SilentlyContinue
if (-not $gitCmd) {
    Write-Error "[FAIL] git was not found on PATH. Install Git for Windows from https://git-scm.com/download/win, then re-run this script."
    exit 1
}
Write-Output "[OK] Found: $(& git --version) ($($gitCmd.Source))"

# --- 2. Ensure the repo exists / is up to date ------------------------------

Write-Section "Step 2/7: syncing the repository at $RepoPath"

if (-not (Test-Path -LiteralPath $RepoPath)) {
    Write-Output "[INFO] '$RepoPath' does not exist yet; cloning $OriginUrl ..."
    $parent = Split-Path -Parent $RepoPath
    if ($parent -and -not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }
    & git clone $OriginUrl $RepoPath
    if ($LASTEXITCODE -ne 0) {
        Write-Error "[FAIL] git clone failed (exit $LASTEXITCODE). Check network/credentials and re-run."
        exit 1
    }
    Write-Output "[OK] Cloned into $RepoPath"
} else {
    if (-not (Test-Path -LiteralPath (Join-Path $RepoPath ".git"))) {
        Write-Error "[FAIL] '$RepoPath' exists but is not a git repository. Pass -RepoPath pointing at a real DarkFac clone, or an empty/non-existent path to clone fresh."
        exit 1
    }
    Write-Output "[INFO] Repo already present; running 'git pull' ..."
    & git -C $RepoPath pull
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "[WARN] git pull failed (exit $LASTEXITCODE) -- continuing with whatever is currently checked out. Resolve manually (e.g. local changes blocking the pull) and re-run this script to update."
        $overallOk = $false
    } else {
        Write-Output "[OK] Repo up to date."
    }
}

$RepoPath = (Resolve-Path -LiteralPath $RepoPath).Path
$remoteWorkerScript = Join-Path $RepoPath "core\harness\remote_worker.py"
if (-not (Test-Path -LiteralPath $remoteWorkerScript)) {
    Write-Error "[FAIL] Cannot find '$remoteWorkerScript'. Is -RepoPath pointing at a real DarkFac checkout?"
    exit 1
}

# --- 3. Install/update Python dependencies ----------------------------------

Write-Section "Step 3/7: installing Python dependencies"

$requirementsFile = Join-Path $RepoPath "requirements.txt"
if (Test-Path -LiteralPath $requirementsFile) {
    & python -m pip install -q -r $requirementsFile
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "[WARN] 'pip install -r requirements.txt' exited with code $LASTEXITCODE. The worker may fail to start if a dependency (fastapi/uvicorn) is missing -- re-run 'python -m pip install -r requirements.txt' manually inside $RepoPath to see the full error."
        $overallOk = $false
    } else {
        Write-Output "[OK] Dependencies installed/up to date."
    }
} else {
    Write-Warning "[WARN] '$requirementsFile' not found; skipping dependency install."
}

# --- 4. Register the Scheduled Task -----------------------------------------

Write-Section "Step 4/7: registering the Scheduled Task '$TaskName'"

$pythonwCmd = Get-Command pythonw -ErrorAction SilentlyContinue
$launchExe = if ($pythonwCmd) { $pythonwCmd.Source } else { $pythonCmd.Source }
$launchArgs = "core\harness\remote_worker.py --host 0.0.0.0 --port $Port --node-id $NodeId"
if (-not $pythonwCmd) {
    Write-Output "[INFO] pythonw.exe not found next to python.exe; the task will run python.exe with a hidden window instead (functionally identical, no visible console)."
}

$action = New-ScheduledTaskAction `
    -Execute $launchExe `
    -Argument $launchArgs `
    -WorkingDirectory $RepoPath

$triggers = @(
    (New-ScheduledTaskTrigger -AtLogOn),
    (New-ScheduledTaskTrigger -AtStartup)
)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Days 3650) `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -Hidden

$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existingTask) {
    Write-Output "[INFO] Task '$TaskName' already exists; replacing it with the current configuration."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $triggers `
    -Settings $settings `
    -Description "DarkFac primary validation-harness test worker (core/harness/remote_worker.py, port $Port). Registered by scripts/install_test_worker.ps1 -- see docs/runbooks/desktop_test_worker.md." `
    -Force | Out-Null

Write-Output "[OK] Task '$TaskName' registered: runs '$launchExe $launchArgs' from '$RepoPath', at logon and at startup, hidden, auto-restarting on failure."

# --- 5. Firewall rule (needs Administrator) ---------------------------------

Write-Section "Step 5/7: inbound firewall rule for TCP $Port (Tailscale-only)"

$firewallRuleName = "DarkFac Test Worker (TCP $Port, Tailscale)"
if (Test-IsAdministrator) {
    $existingRule = Get-NetFirewallRule -DisplayName $firewallRuleName -ErrorAction SilentlyContinue
    if ($existingRule) {
        Write-Output "[INFO] Firewall rule already exists; replacing it."
        Remove-NetFirewallRule -DisplayName $firewallRuleName -ErrorAction SilentlyContinue
    }
    New-NetFirewallRule `
        -DisplayName $firewallRuleName `
        -Direction Inbound `
        -Protocol TCP `
        -LocalPort $Port `
        -RemoteAddress $TailscaleCidr `
        -Action Allow `
        -Profile Any | Out-Null
    Write-Output "[OK] Firewall rule '$firewallRuleName' created: inbound TCP $Port allowed only from $TailscaleCidr (the Tailscale CGNAT range)."
} else {
    Write-Warning "[SKIPPED] Not running as Administrator -- the firewall rule was NOT created. Without it, Windows Defender Firewall will likely block incoming connections from other tailnet hosts (VPS/Notebook) on port $Port."
    Write-Output "  To finish this step, do ONE of the following:"
    Write-Output "    (a) Re-run this entire script from an elevated PowerShell window"
    Write-Output "        (right-click PowerShell -> 'Run as administrator', then re-run the same command)."
    Write-Output "    (b) Or run just this command from an elevated PowerShell window:"
    Write-Output "        New-NetFirewallRule -DisplayName '$firewallRuleName' -Direction Inbound -Protocol TCP -LocalPort $Port -RemoteAddress '$TailscaleCidr' -Action Allow -Profile Any"
    $overallOk = $false
}

# --- 6. Optional auth token --------------------------------------------------

Write-Section "Step 6/7: optional DARKFAC_WORKER_TOKEN"

if ($Token) {
    [System.Environment]::SetEnvironmentVariable("DARKFAC_WORKER_TOKEN", $Token, "User")
    [System.Environment]::SetEnvironmentVariable("DARKFAC_WORKER_TOKEN", $Token, "Machine")
    $env:DARKFAC_WORKER_TOKEN = $Token
    Write-Output "[OK] DARKFAC_WORKER_TOKEN set (user + machine environment). The task must be restarted (see Step 7) to pick it up; already-open terminals will only see it after reopening."
} else {
    Write-Output "[INFO] No -Token passed; DARKFAC_WORKER_TOKEN left as-is. The worker's mutating endpoints (job submission, /system/exec, /system/update, /system/restart) stay unauthenticated -- access is still restricted to the tailnet by the firewall rule above. Pass -Token '<a-long-random-string>' to require a bearer token."
}

# --- 7. Start now and verify -------------------------------------------------

Write-Section "Step 7/7: starting the worker now and verifying health"

Start-ScheduledTask -TaskName $TaskName
Write-Output "[INFO] Task started. Waiting for http://127.0.0.1:$Port/health to respond ..."

$healthOk = $false
$deadline = (Get-Date).AddSeconds(20)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 1
    try {
        $response = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3 -ErrorAction Stop
        if ($response.status) {
            $healthOk = $true
            Write-Output "[OK] Worker responded: status=$($response.status) node_id=$($response.node_id) hostname=$($response.hostname) platform_family=$($response.platform_family)"
            break
        }
    } catch {
        # keep polling until the deadline
    }
}

if (-not $healthOk) {
    Write-Warning "[FAIL] http://127.0.0.1:$Port/health did not respond within 20s. Check Task Scheduler (see docs/runbooks/desktop_test_worker.md, 'Troubleshooting') -- most often a missing dependency (Step 3) or the task not actually starting."
    $overallOk = $false
}

# --- Summary ------------------------------------------------------------------

Write-Output ""
Write-Output "======================================================================"
Write-Output " SUMMARY"
Write-Output "======================================================================"
Write-Output " Repo path        : $RepoPath"
Write-Output " Scheduled Task   : $TaskName (Task Scheduler Library root, triggers: at logon + at startup)"
Write-Output " Worker command   : $launchExe $launchArgs"
Write-Output " Local health URL : http://127.0.0.1:$Port/health"
Write-Output " Tailnet URL      : http://100.78.181.90:$Port/health  (from another tailnet host)"
Write-Output " Firewall rule    : $(if (Test-IsAdministrator) { 'created (TCP ' + $Port + ' from ' + $TailscaleCidr + ')' } else { 'NOT created -- see Step 5 above' })"
Write-Output " Auth token       : $(if ($Token) { 'set' } else { 'not set (open on tailnet)' })"
if ($overallOk) {
    Write-Output " Result           : OK -- the Desktop is ready as the primary test worker."
} else {
    Write-Output " Result           : COMPLETED WITH WARNINGS -- see the [WARN]/[SKIPPED]/[FAIL] lines above."
}
Write-Output "======================================================================"

if (-not $overallOk) {
    exit 1
}
exit 0
