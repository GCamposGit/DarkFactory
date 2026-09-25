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

# Windows PowerShell 5.1 turns a native command's redirected stderr into a
# terminating NativeCommandError under ErrorActionPreference=Stop (e.g.
# "py -3.13" when that version is absent). Probes only need stdout + exit code.
function Invoke-NativeProbe {
    param([string]$Exe, [string[]]$Arguments)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        return (& $Exe @Arguments 2>$null)
    } finally {
        $ErrorActionPreference = $previous
    }
}

# The worker must come back after a reboot of a headless Desktop even when
# nobody logs on, which needs a "run whether the user is logged on or not"
# (S4U) task, and the Tailscale-only firewall rule; both require elevation.
if (-not (Test-IsAdministrator)) {
    Write-Output "[ERROR] This installer must run from an elevated PowerShell."
    Write-Output "        Close this window, right-click 'Windows PowerShell' -> 'Run as administrator',"
    Write-Output "        and run the same command again. Nothing was changed."
    exit 1
}

Write-Output "======================================================================"
Write-Output " DarkFac Primary Test Worker Installer (HF-27-11)"
Write-Output " Repo path : $RepoPath"
Write-Output " Node id   : $NodeId"
Write-Output " Port      : $Port"
Write-Output "======================================================================"

# --- 1. Verify Python 3.12+ and git -----------------------------------------

Write-Section "Step 1/7: checking prerequisites (Python 3.12+, git)"

# Harness evidence must come from the Python line CI uses (3.12+). The worker
# gets its own venv from that interpreter, so an older global Python used by
# other apps (e.g. 3.11) stays installed and untouched.
$pythonExe = $null
$pyLauncher = Get-Command py -ErrorAction SilentlyContinue
if ($pyLauncher) {
    foreach ($selector in @("-3.12", "-3.13")) {
        $candidate = Invoke-NativeProbe $pyLauncher.Source @($selector, "-c", "import sys; print(sys.executable)")
        if ($LASTEXITCODE -eq 0 -and $candidate) { $pythonExe = "$candidate".Trim(); break }
    }
}
if (-not $pythonExe) {
    $pythonOnPath = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonOnPath) {
        Invoke-NativeProbe $pythonOnPath.Source @("-c", "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)") | Out-Null
        if ($LASTEXITCODE -eq 0) { $pythonExe = $pythonOnPath.Source }
    }
}
if (-not $pythonExe) {
    Write-Output "[FAIL] Python 3.12 was not found (the global 'python' may be older, e.g. 3.11)."
    Write-Output "       Install it ALONGSIDE the current one (other apps keep their Python):"
    Write-Output "         winget install -e --id Python.Python.3.12"
    Write-Output "       Then close this window, open a NEW elevated PowerShell and re-run this script."
    exit 1
}
$pythonCmd = Get-Command $pythonExe
Write-Output "[OK] Found: $(& $pythonExe --version) ($pythonExe)"

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
        # Continuing would install and start a stale worker that can look healthy.
        Write-Output "[FAIL] git pull failed (exit $LASTEXITCODE); stopping so a stale worker is not installed."
        Write-Output "       If it says 'local changes ... would be overwritten', see what changed with:"
        Write-Output "         git -C $RepoPath status --short"
        Write-Output "       and set those edits aside (recoverable later with 'git stash list'):"
        Write-Output "         git -C $RepoPath stash push -u -m desktop-local-changes"
        Write-Output "       then re-run this script."
        exit 1
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

Write-Section "Step 3/7: installing Python dependencies into the worker's own venv"

# A dedicated venv keeps DarkFac's pins (e.g. Pillow<12) from downgrading
# packages other apps on this machine use from the global Python
# (open-webui, pdfplumber, ...). Harness jobs inherit it via sys.executable.
$VenvPath = Join-Path $env:LOCALAPPDATA "DarkFac\worker\venv"
$venvPython = Join-Path $VenvPath "Scripts\python.exe"
if (Test-Path -LiteralPath $venvPython) {
    Invoke-NativeProbe $venvPython @("-c", "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)") | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Output "[INFO] Existing worker venv uses Python < 3.12; recreating it."
        Remove-Item -Recurse -Force -LiteralPath $VenvPath
    }
}
if (-not (Test-Path -LiteralPath $venvPython)) {
    & $pythonCmd.Source -m venv $VenvPath
    if ($LASTEXITCODE -ne 0) {
        Write-Output "[ERROR] Could not create the worker venv at '$VenvPath'."
        exit 1
    }
    Write-Output "[OK] Created worker venv: $VenvPath"
} else {
    Write-Output "[OK] Reusing worker venv: $VenvPath"
}

$requirementsFile = Join-Path $RepoPath "requirements.txt"
if (Test-Path -LiteralPath $requirementsFile) {
    & $venvPython -m pip install -q --upgrade pip
    & $venvPython -m pip install -q -r $requirementsFile
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "[WARN] 'pip install -r requirements.txt' into the worker venv exited with code $LASTEXITCODE. Re-run '$venvPython -m pip install -r requirements.txt' inside $RepoPath to see the full error."
        $overallOk = $false
    } else {
        Write-Output "[OK] Dependencies installed in the worker venv (the global Python was not touched)."
    }
} else {
    Write-Warning "[WARN] '$requirementsFile' not found; skipping dependency install."
}

# --- 4. Register the Scheduled Task -----------------------------------------

Write-Section "Step 4/7: registering the Scheduled Task '$TaskName'"

# The worker redirects its console-less stdout/stderr to
# %LOCALAPPDATA%\DarkFac\worker\daemon.log (see ensure_console_streams).
$venvPythonw = Join-Path $VenvPath "Scripts\pythonw.exe"
$launchExe = if (Test-Path -LiteralPath $venvPythonw) { $venvPythonw } else { $venvPython }
$launchArgs = "core\harness\remote_worker.py --host 0.0.0.0 --port $Port --node-id $NodeId"

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

# S4U = "Run whether user is logged on or not" without storing a password:
# the worker starts at boot on a headless Desktop, under the owner's account
# (so it sees the same Python, repo and harness CLI logins).
$principal = New-ScheduledTaskPrincipal `
    -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType S4U `
    -RunLevel Limited

# scripts/install_test_worker_startup.ps1 installs an alternative logon-only
# launcher in the Startup folder; two launchers would fight over the port.
$legacyLauncher = Join-Path ([System.Environment]::GetFolderPath([System.Environment+SpecialFolder]::Startup)) "DarkFacTestWorker.vbs"
if (Test-Path -LiteralPath $legacyLauncher) {
    Remove-Item -LiteralPath $legacyLauncher -Force
    Write-Output "[INFO] Removed the Startup-folder launcher '$legacyLauncher'; the scheduled task below replaces it."
}

$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existingTask) {
    Write-Output "[INFO] Task '$TaskName' already exists; replacing it with the current configuration."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $triggers `
    -Principal $principal `
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

# An older worker (manual start or Startup-folder launcher) would keep the
# port and answer the health check with stale code.
$staleWorkers = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -match 'remote_worker\.py' }
foreach ($stale in $staleWorkers) {
    Stop-Process -Id $stale.ProcessId -Force -ErrorAction SilentlyContinue
    Write-Output "[INFO] Stopped an already-running worker process (pid $($stale.ProcessId))."
}

Start-ScheduledTask -TaskName $TaskName
Write-Output "[INFO] Task started. Waiting for http://127.0.0.1:$Port/health to respond ..."

$healthOk = $false
$deadline = (Get-Date).AddSeconds(20)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 1
    try {
        $response = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3 -ErrorAction Stop
        # Only the new worker reports platform_family; an old one must not pass.
        if ($response.status -and $response.platform_family) {
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
