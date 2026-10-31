# Runner autonomo do Sentinela da Linha DarkFac (USR-439).
# Executa a verificacao da esteira e cotas a cada 45 minutos.
param (
    [switch]$Force,
    [switch]$Quiet,
    [double]$IntervalSec = 2700.0
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptDir

$pythonCmd = Get-Command python.exe -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
    Write-Error "Python nao encontrado no PATH."
    exit 1
}

$argsList = @("-m", "core.line.sentinel", "--interval-sec", "$IntervalSec")
if ($Force) { $argsList += "--force" }
if ($Quiet) { $argsList += "--quiet" }

Push-Location $repoRoot
try {
    & $pythonCmd.Source $argsList
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
