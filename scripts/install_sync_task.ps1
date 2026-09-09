# ==============================================================================
# Instala tarefa em segundo plano no Windows para sincronizar quotas e saldos
# Executa de forma invisível (WindowStyle: Hidden) sem intervenção manual
# ==============================================================================

param (
    [string]$TargetUrl = "https://darkhub.ggcampos.com",
    [string]$Key = "",
    [int]$IntervalMinutes = 15
)

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptDir
$syncScript = Join-Path $repoRoot "scripts\sync_usage_to_cloud.py"

if (-not (Test-Path $syncScript)) {
    Write-Error "Script de sincronização não encontrado em $syncScript"
    exit 1
}

# Se a chave não foi passada, tenta ler do .env local
if ([string]::IsNullOrWhiteSpace($Key)) {
    $envFile = Join-Path $repoRoot ".env"
    if (Test-Path $envFile) {
        $lines = Get-Content $envFile
        foreach ($line in $lines) {
            if ($line -match "^DARKFAC_TELEMETRY_KEY=(.*)$") {
                $Key = $matches[1].Trim()
                break
            }
        }
    }
}

if ([string]::IsNullOrWhiteSpace($Key)) {
    Write-Warning "Atenção: DARKFAC_TELEMETRY_KEY não informada. Passe -Key ou configure no .env."
}

$taskName = "DarkFac-Usage-Cloud-Sync"
$pythonPath = (Get-Command python.exe).Source

$actionArgs = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -Command `"`"$pythonPath`" `"$syncScript`" --target-url `"$TargetUrl`" --key `"$Key`"`""
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $actionArgs
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null

Write-Host "[OK] Tarefa agendada '$taskName' instalada com sucesso no Windows!" -ForegroundColor Green
Write-Host "     Sincronização automática a cada $IntervalMinutes minutos em segundo plano (invisível)."
Write-Host "     Você nunca mais precisará rodar scripts manualmente no terminal."
