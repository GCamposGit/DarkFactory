# Installs a hidden, per-user logon task for continuous DarkHub quota synchronization.
param (
    [string]$TargetUrl = "https://darkhub.ggcampos.com"
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptDir
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
