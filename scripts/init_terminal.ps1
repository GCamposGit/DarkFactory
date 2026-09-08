<#
.SYNOPSIS
    DarkFac Terminal Environment Initialization and Guard Script.

.DESCRIPTION
    Ensures that the current PowerShell session or subprocess is correctly anchored
    in the DarkFac repository root, neutralizing unauthorized directory shifts caused
    by global profiles (e.g. C:\Windows\System32\WindowsPowerShell\v1.0\Microsoft.PowerShell_profile.ps1).
    Configures UTF-8 encoding across console streams and Python runtime.

.PARAMETER RootPath
    Explicit path to the DarkFac root. If omitted, discovers root from script location.

.PARAMETER CheckOnly
    If set, validates the environment without modifying session state.

.PARAMETER Quiet
    Suppresses informational messages, outputting only deterministic markers.
#>

[CmdletBinding()]
param (
    [string]$RootPath = "",
    [switch]$CheckOnly,
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"

# 1. Discover Project Root
if ([string]::IsNullOrWhiteSpace($RootPath)) {
    $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
    $candidateRoot = Split-Path -Parent $scriptDir
} else {
    $candidateRoot = (Resolve-Path -LiteralPath $RootPath).Path
}

# Verify anchor files
$anchorFiles = @("MISSION.md", "FACTORY_RULES.md", "harness.config.json", "core/harness/runner.py")
$missingAnchors = @()

foreach ($anchor in $anchorFiles) {
    $fullPath = Join-Path $candidateRoot $anchor
    if (-not (Test-Path -LiteralPath $fullPath)) {
        $missingAnchors += $anchor
    }
}

if ($missingAnchors.Count -gt 0) {
    Write-Error "[TERMINAL_INIT_FAIL] Target path '$candidateRoot' is missing anchor files: $($missingAnchors -join ', ')"
    exit 1
}

# 2. Fix / Anchor Working Directory
$previousCwd = (Get-Location).Path
if (-not $CheckOnly) {
    if ($previousCwd -ne $candidateRoot) {
        Set-Location -LiteralPath $candidateRoot
    }

    # 3. Configure UTF-8 and Environment Variables
    $env:DARKFAC_ROOT = $candidateRoot
    $env:PYTHONIOENCODING = "utf-8"
    $env:PYTHONUTF8 = "1"

    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    [Console]::InputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
}

$currentCwd = (Get-Location).Path
$status = if ($currentCwd -eq $candidateRoot) { "ALIGNED" } else { "MISALIGNED" }

# 4. Output Deterministic Markers
Write-Output "[TERMINAL_INIT_OK] DarkFac terminal environment anchored at $candidateRoot (Status: $status)"

if (-not $Quiet) {
    Write-Output "  Previous CWD : $previousCwd"
    Write-Output "  Current CWD  : $currentCwd"
    Write-Output "  DARKFAC_ROOT : $env:DARKFAC_ROOT"
    Write-Output "  Encoding     : UTF-8 (Console & Python)"
    Write-Output "  Anchors      : $($anchorFiles.Count) verified"
}

# 5. Define Guard Helper Function in Session
function global:Invoke-GuardedPowerShell {
    param([Parameter(Mandatory=$true)][string]$Command)
    & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command $Command
}
