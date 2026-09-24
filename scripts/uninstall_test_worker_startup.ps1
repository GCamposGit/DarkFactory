<#
.SYNOPSIS
    Removes the DarkFac Remote Test Worker startup script.
#>

[CmdletBinding()]
param ()

$startupFolder = [System.Environment]::GetFolderPath([System.Environment+SpecialFolder]::Startup)
$vbsPath = Join-Path $startupFolder "DarkFacTestWorker.vbs"

if (Test-Path -LiteralPath $vbsPath) {
    Remove-Item -Path $vbsPath -Force
    Write-Output "[UNINSTALL_OK] Removed $vbsPath"
} else {
    Write-Output "[UNINSTALL_INFO] No startup script found at $vbsPath"
}
