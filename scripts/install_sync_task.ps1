# Installs a hidden, per-user logon task for continuous DarkHub quota synchronization.
param (
    [string]$TargetUrl = "https://darkhub.ggcampos.com",
    # Executa apenas a guarda de worktree e sai com 0, sem registrar tarefa (usado em testes).
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptDir

# Guarda USR-240: nunca registrar a tarefa a partir de uma worktree (incidente 03-10/10/2026).
# Deve rodar antes de qualquer efeito colateral.
$correctCommand = 'powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File C:\dev\DarkFac\scripts\install_sync_task.ps1'
$normalizedRoot = ($repoRoot -replace '/', '\').ToLowerInvariant()
$isWorktreePath = $false
foreach ($marker in @('\.worktrees', '\.claude\worktrees', '\.codex\worktrees')) {
    if (($normalizedRoot + '').Contains($marker)) { $isWorktreePath = $true }
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
        $a = ([string]$gitDir -replace '/', '').TrimEnd('').ToLowerInvariant()
        $b = ([string]$gitCommon -replace '/', '').TrimEnd('').ToLowerInvariant()
        if ($a -ne $b) { $isSecondaryWorktree = $true }
    }
}
if ($isWorktreePath -or $isSecondaryWorktree) {
    [Console]::Error.WriteLine("RECUSADO: a raiz '$repoRoot' é uma worktree; a tarefa agendada não pode apontar para ela.")
    [Console]::Error.WriteLine("Execute a partir do checkout principal: $correctCommand")
    exit 2
}
if ($DryRun) {
    Write-Output "[DRY-RUN] Guarda de worktree aprovada para $repoRoot; nenhuma tarefa registrada."
    exit 0
}
$pythonPath = (Get-Command python.exe -ErrorAction Stop).Source
& $pythonPath (Join-Path $repoRoot "scripts\install_usage_sync_task.py") --apply --target-url $TargetUrl
exit $LASTEXITCODE
