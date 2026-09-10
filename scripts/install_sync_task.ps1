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

# Localiza pythonw.exe (GUI subsystem) para execução 100% silenciosa/headless sem janela de console
$pythonwCmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
if ($pythonwCmd) {
    $pythonwPath = $pythonwCmd.Source
} else {
    $pythonCmd = Get-Command python.exe -ErrorAction Stop
    $candidate = Join-Path (Split-Path -Parent $pythonCmd.Source) "pythonw.exe"
    if (Test-Path $candidate) {
        $pythonwPath = $candidate
    } else {
        $pythonwPath = $pythonCmd.Source
    }
}

$actionArgs = "`"$syncScript`" --target-url `"$TargetUrl`" --key `"$Key`""
$action = New-ScheduledTaskAction -Execute $pythonwPath -Argument $actionArgs
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null

Write-Host "[OK] Tarefa agendada '$taskName' atualizada com sucesso para modo 100% HEADLESS (pythonw.exe)!" -ForegroundColor Green
Write-Host "     Binário: $pythonwPath"
Write-Host "     Sincronização automática a cada $IntervalMinutes minutos em segundo plano absoluto (zero janelas/flashes)."
Write-Host "     Você nunca mais será interrompido por popups ou janelas do shell."
