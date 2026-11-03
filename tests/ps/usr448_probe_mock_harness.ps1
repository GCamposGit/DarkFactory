# USR-448: mocks every ScheduledTask/Get-Command surface used by install_availability_probe.ps1
# and dot-sources the real script so tests/test_hub_deploy_usr207.py can assert on fail-closed
# resolution, action wiring and idempotent registration without touching the real Task Scheduler.
param (
    [Parameter(Mandatory = $true)][string]$ScriptPath,
    [string]$TaskName = "DarkFac-Hub-Availability",
    [switch]$Verify,
    [switch]$DryRun
)

function Get-Command {
    param($Name, $ErrorAction)
    if ($Name -eq "pythonw.exe") {
        if ($env:FAKE_PYTHONW_PATH) { return [PSCustomObject]@{ Source = $env:FAKE_PYTHONW_PATH } }
        return $null
    }
    if ($Name -eq "python.exe") {
        if ($env:FAKE_PYTHON_PATH) { return [PSCustomObject]@{ Source = $env:FAKE_PYTHON_PATH } }
        return $null
    }
    # git: always report absent so the secondary-worktree check never shells out in the harness.
    return $null
}

function New-ScheduledTaskAction {
    param($Execute, $Argument, $WorkingDirectory)
    # Write-Host (not Write-Output): the real script assigns this call's return value to a
    # variable and pipes Register-ScheduledTask's into Out-Null, so anything put on the success
    # stream here would be silently absorbed instead of reaching the test's captured stdout.
    Write-Host "CALL:New-ScheduledTaskAction:Execute=$Execute|Argument=$Argument|WorkingDirectory=$WorkingDirectory"
    return [PSCustomObject]@{ Execute = $Execute; Argument = $Argument; WorkingDirectory = $WorkingDirectory }
}

function New-ScheduledTaskTrigger {
    param([switch]$Once, $At, $RepetitionInterval)
    return [PSCustomObject]@{}
}

function Register-ScheduledTask {
    param($TaskName, $Action, $Trigger, [switch]$Force)
    Write-Host "CALL:Register-ScheduledTask:TaskName=$TaskName|Force=$($Force.IsPresent)"
    return $null
}

function Get-ScheduledTask {
    param($TaskName, $ErrorAction)
    if ($env:FAKE_TASK_REGISTERED -eq "1") {
        return [PSCustomObject]@{
            TaskName = $TaskName
            Actions  = @([PSCustomObject]@{
                Execute          = $env:FAKE_PYTHONW_PATH
                Arguments        = "-m core.infra.availability_probe"
                WorkingDirectory = $env:FAKE_REPO_ROOT
            })
        }
    }
    return $null
}

function Get-ScheduledTaskInfo {
    param($TaskName)
    $result = 0
    if ($env:FAKE_LAST_RESULT) { $result = [int]$env:FAKE_LAST_RESULT }
    return [PSCustomObject]@{ LastTaskResult = $result }
}

$forwarded = @{ TaskName = $TaskName }
if ($Verify) { $forwarded.Verify = $true }
if ($DryRun) { $forwarded.DryRun = $true }

$Global:LASTEXITCODE = 0
. $ScriptPath @forwarded
# `exit` inside a dot-sourced script only unwinds to this scope under -File; it does not end the
# harness process. Re-exit with the real script's own $LASTEXITCODE so the test's returncode is real.
exit $LASTEXITCODE
