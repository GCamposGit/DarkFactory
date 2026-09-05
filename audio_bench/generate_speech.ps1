# Script PowerShell para sintetizar vozes reais de reunião em PT-BR e EN
param(
    [string]$OutputDir = "audio_bench"
)

if (!(Test-Path $OutputDir)) {
    New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
}

Add-Type -AssemblyName System.Speech

# Sintetizar Maria (PT-BR)
$synthMaria = New-Object System.Speech.Synthesis.SpeechSynthesizer
$synthMaria.SelectVoice("Microsoft Maria Desktop")
$synthMaria.Rate = 0
$synthMaria.Volume = 100
$mariaPath = Join-Path $OutputDir "maria_pt.wav"
$synthMaria.SetOutputToWaveFile($mariaPath)
$synthMaria.Speak("Olá pessoal, bem-vindos à nossa reunião de alinhamento técnico. Vamos revisar a arquitetura dos agentes e a validação dos modelos locais no ambiente Dark Factory.")
$synthMaria.Dispose()

# Sintetizar David (EN-US)
$synthDavid = New-Object System.Speech.Synthesis.SpeechSynthesizer
$synthDavid.SelectVoice("Microsoft David Desktop")
$synthDavid.Rate = 0
$synthDavid.Volume = 100
$davidPath = Join-Path $OutputDir "david_en.wav"
$synthDavid.SetOutputToWaveFile($davidPath)
$synthDavid.Speak("Thank you Maria. I reviewed the latest pull request and the pipeline latency looks very promising, but we must verify memory pressure under concurrent workloads.")
$synthDavid.Dispose()

Write-Host "Vozes sintetizadas com sucesso em: $OutputDir"
