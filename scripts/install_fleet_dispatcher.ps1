# Installs the boot-triggered fleet dispatcher task (USR-384).
param ([switch]$DryRun)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptDir

# Never register a machine task from an isolated worktree.
$correctCommand = 'powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File C:\dev\DarkFac\scripts\install_fleet_dispatcher.ps1'
$normalizedRoot = ($repoRoot -replace '/', '\').ToLowerInvariant()
$isWorktreePath = $normalizedRoot.Contains('\.worktrees') -or $normalizedRoot.Contains('\.claude\worktrees') -or $normalizedRoot.Contains('\.codex\worktrees')
$gitCmd = Get-Command git -ErrorAction SilentlyContinue
$isSecondaryWorktree = $false
if ($gitCmd) {
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $gitDir = & git -C $repoRoot rev-parse --absolute-git-dir 2>$null
        $gitDirRc = $LASTEXITCODE
        $gitCommon = & git -C $repoRoot rev-parse --path-format=absolute --git-common-dir 2>$null
        $gitCommonRc = $LASTEXITCODE
        if ($gitDirRc -eq 0 -and $gitCommonRc -eq 0 -and $gitDir -and $gitCommon) {
            $normalizedGitDir = (([string]$gitDir).Trim() -replace '/', '\').TrimEnd('\').ToLowerInvariant()
            $normalizedGitCommon = (([string]$gitCommon).Trim() -replace '/', '\').TrimEnd('\').ToLowerInvariant()
            $isSecondaryWorktree = $normalizedGitDir -ne $normalizedGitCommon
        }
    } finally { $ErrorActionPreference = $previousPreference }
}
if ($isWorktreePath -or $isSecondaryWorktree) {
    [Console]::Error.WriteLine("RECUSADO: '$repoRoot' e uma worktree. Execute no checkout principal: $correctCommand")
    exit 2
}
if ($DryRun) {
    Write-Output "[DRY-RUN] Guarda de worktree aprovada para $repoRoot; nenhuma tarefa registrada."
    exit 0
}

$pythonPath = (Get-Command python.exe -ErrorAction Stop).Source
$stateRoot = Join-Path $repoRoot ".factory"
$bootstrapPath = Join-Path $repoRoot "core\line\fleet_bootstrap.py"

# USR-471 (T4): never redefine the scheduled task while a ticket is in flight on this host or a
# remote one; the reinstall must preserve the queue and the SHA instead of racing a live run.
Push-Location $repoRoot
try {
    $activeCheckOutput = & $pythonPath -m core.line.fleet_bootstrap --project-root $repoRoot --state-root $stateRoot --check-active-run 2>&1
    $activeCheckRc = $LASTEXITCODE
} finally { Pop-Location }
Write-Output "[CHECK] $activeCheckOutput"
if ($activeCheckRc -ne 0) {
    [Console]::Error.WriteLine("RECUSADO: ha um run ativo no despachante ($activeCheckOutput). Nao reinstale agora; a fila e o SHA sao preservados. Repita depois que o run concluir.")
    exit 3
}

# Register-ScheduledTask -Force replaces the task definition. Do not do that while
# its current instance is still running, even if the last persisted cycle is idle.
$existingTask = Get-ScheduledTask -TaskName "DarkFac-Fleet-Dispatcher" -ErrorAction SilentlyContinue
if ($null -ne $existingTask -and [string]$existingTask.State -eq "Running") {
    [Console]::Error.WriteLine("RECUSADO: a tarefa DarkFac-Fleet-Dispatcher ainda esta em execucao. Aguarde o processo terminar antes de reinstalar; o host nao sera reiniciado nem a instancia substituida.")
    exit 4
}

$userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$credential = Get-Credential -UserName $userId -Message "Credencial da conta de servico do despachante (inicia no boot)"
if ($null -eq $credential) { throw "Instalacao cancelada: a credencial da conta e obrigatoria." }
$plainPassword = $credential.GetNetworkCredential().Password
try {
    Push-Location $repoRoot
    try {
        $xmlLines = & $pythonPath -m core.line.fleet_task_xml `
            --project-root $repoRoot `
            --state-root $stateRoot `
            --python $pythonPath `
            --bootstrap $bootstrapPath
        if ($LASTEXITCODE -ne 0) { throw "Nao foi possivel gerar o XML da tarefa" }
    } finally { Pop-Location }
    Register-ScheduledTask -TaskName "DarkFac-Fleet-Dispatcher" -Xml ($xmlLines -join "`n") `
        -User $userId -Password $plainPassword -Force | Out-Null
} finally {
    $plainPassword = $null
    $credential = $null
    $xmlLines = $null
}
Write-Output "[OK] Tarefa 'DarkFac-Fleet-Dispatcher' registrada (boot, conta de servico, reinicio em falha)."
Write-Output "     Estado: $repoRoot\.factory\runs\fleet_dispatcher_state.json"
