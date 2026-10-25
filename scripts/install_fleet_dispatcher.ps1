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
$userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$credential = Get-Credential -UserName $userId -Message "Credencial da conta de servico do despachante (inicia no boot)"
if ($null -eq $credential) { throw "Instalacao cancelada: a credencial da conta e obrigatoria." }
$plainPassword = $credential.GetNetworkCredential().Password
try {
    Push-Location $repoRoot
    try {
        $xmlLines = & $pythonPath -m core.line.fleet_task_xml --user $userId --python $pythonPath --repo $repoRoot
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
