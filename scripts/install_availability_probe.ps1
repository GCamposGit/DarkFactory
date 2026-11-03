# USR-207/USR-448: registers (idempotently) and verifies the Hub availability probe
# scheduled task. Replaces the manual snippet previously copy-pasted from the proposal.
param (
    [string]$TaskName = "DarkFac-Hub-Availability",
    # Reports whether the task is registered, what it executes and its last result code;
    # never registers or modifies anything.
    [switch]$Verify,
    # Runs the worktree guard and pythonw.exe resolution but exits before registering
    # (used by tests).
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptDir
$correctCommand = 'powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File C:\dev\DarkFac\scripts\install_availability_probe.ps1'

if ($Verify) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        Write-Output "[VERIFY] Tarefa '$TaskName' nao esta registrada."
        exit 1
    }
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    $action = $task.Actions | Select-Object -First 1
    Write-Output "[VERIFY] Tarefa '$TaskName' registrada."
    Write-Output "         Executa: $($action.Execute) $($action.Arguments)"
    Write-Output "         WorkingDirectory: $($action.WorkingDirectory)"
    Write-Output "         LastTaskResult: $($info.LastTaskResult)"
    if ($info.LastTaskResult -ne 0) {
        Write-Output "[VERIFY] FALHOU: LastTaskResult != 0."
        exit 1
    }
    exit 0
}

# Guarda: nunca registrar a tarefa a partir de uma worktree (a WorkingDirectory precisa
# ser o checkout principal; mesmo incidente de padrao do USR-240).
$normalizedRoot = ($repoRoot -replace '/', '\').ToLowerInvariant()
$isWorktreePath = $false
foreach ($marker in @('\.worktrees', '\.claude\worktrees', '\.codex\worktrees')) {
    if ($normalizedRoot.Contains($marker)) { $isWorktreePath = $true }
}
$isSecondaryWorktree = $false
$gitCmd = Get-Command git -ErrorAction SilentlyContinue
if ($gitCmd) {
    # PS 5.1 transforma stderr de executavel nativo em erro terminante sob 'Stop'; tolerar aqui.
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $gitDir = & git -C $repoRoot rev-parse --absolute-git-dir 2>$null
        $rc1 = $LASTEXITCODE
        $gitCommon = & git -C $repoRoot rev-parse --path-format=absolute --git-common-dir 2>$null
        $rc2 = $LASTEXITCODE
    } catch {
        $rc1 = 1
        $rc2 = 1
    } finally {
        $ErrorActionPreference = $prevEap
    }
    if ($rc1 -eq 0 -and $rc2 -eq 0 -and $gitDir -and $gitCommon) {
        $a = ([string]$gitDir -replace '/', '\').TrimEnd('\').ToLowerInvariant()
        $b = ([string]$gitCommon -replace '/', '\').TrimEnd('\').ToLowerInvariant()
        if ($a -ne $b) { $isSecondaryWorktree = $true }
    }
}
if ($isWorktreePath -or $isSecondaryWorktree) {
    [Console]::Error.WriteLine("RECUSADO: a raiz '$repoRoot' e uma worktree; a tarefa agendada nao pode apontar para ela.")
    [Console]::Error.WriteLine("Execute a partir do checkout principal: $correctCommand")
    exit 2
}

if ($DryRun) {
    Write-Output "[DRY-RUN] Guarda de worktree aprovada para $repoRoot; nenhuma tarefa registrada."
    exit 0
}

# Pythonw.exe absoluto; falha fechada se nao existir (nunca abre janela via python.exe).
$pythonwCmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
if ($pythonwCmd) {
    $pythonwPath = $pythonwCmd.Source
} else {
    $pythonCmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $pythonCmd) {
        [Console]::Error.WriteLine("RECUSADO: nem pythonw.exe nem python.exe foram encontrados no PATH.")
        exit 1
    }
    $candidate = Join-Path (Split-Path -Parent $pythonCmd.Source) "pythonw.exe"
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
        [Console]::Error.WriteLine("RECUSADO: pythonw.exe nao encontrado em $candidate; instale o runtime Python completo para executar a tarefa sem janela.")
        exit 1
    }
    $pythonwPath = $candidate
}

$action = New-ScheduledTaskAction -Execute $pythonwPath -Argument "-m core.infra.availability_probe" -WorkingDirectory $repoRoot
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Force | Out-Null

Write-Output "[OK] Tarefa '$TaskName' registrada (idempotente)."
Write-Output "     Executa: $pythonwPath -m core.infra.availability_probe"
Write-Output "     WorkingDirectory: $repoRoot"
Write-Output "     Verifique com: $correctCommand -Verify"
