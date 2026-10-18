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
$syncScript = Join-Path $repoRoot "scripts\sync_usage_to_cloud.py"

if (-not (Test-Path -LiteralPath $syncScript -PathType Leaf)) {
    Write-Error "Script de sincronização não encontrado em $syncScript"
    exit 1
}

# The client reads DARKFAC_TELEMETRY_KEY from the user environment or .env at runtime.
# Never copy credential material into Task Scheduler arguments or task metadata.
$pythonwCmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
if ($pythonwCmd) {
    $pythonwPath = $pythonwCmd.Source
} else {
    $pythonCmd = Get-Command python.exe -ErrorAction Stop
    $candidate = Join-Path (Split-Path -Parent $pythonCmd.Source) "pythonw.exe"
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
        Write-Error "pythonw.exe não encontrado ao lado de python.exe; instale o runtime Python completo para executar a tarefa sem janela."
        exit 1
    }
    $pythonwPath = $candidate
}

$userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$actionArgs = '"{0}" --target-url "{1}" --loop --interval 60' -f $syncScript, $TargetUrl
$action = New-ScheduledTaskAction -Execute $pythonwPath -Argument $actionArgs -WorkingDirectory $repoRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -Hidden `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask `
    -TaskName "DarkFac-Usage-Cloud-Sync" `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Force | Out-Null

Write-Output "[OK] Tarefa per-user 'DarkFac-Usage-Cloud-Sync' registrada para iniciar no logon."
Write-Output "     Execução invisível em loop, sincronização a cada 60 segundos e até 3 reinícios após falha."
Write-Output "     Credencial carregada pelo cliente em runtime via ambiente do usuário ou $repoRoot\.env."
Write-Output "     Diagnósticos locais: $repoRoot\.factory\logs\usage_sync.log"
