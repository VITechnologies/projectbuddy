[CmdletBinding()]
param(
    [string]$CliVersion = "26.3.2",
    [string]$DesktopVersion = "2026.3.2.4056"
)

$ErrorActionPreference = "Stop"
$command = Get-Command vipm -CommandType Application -ErrorAction Stop | Select-Object -First 1
$actual = (& $command.Source --version | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $actual -ne "vipm $CliVersion") {
    throw "VIPM CLI version mismatch at '$($command.Source)': expected 'vipm $CliVersion', got '$actual'. Rebuild the Windows image from the pinned release; do not reuse a stale local image or preview-channel installer."
}
$desktopPath = Join-Path (Split-Path (Split-Path $command.Source)) "VI Package Manager.exe"
if (-not (Test-Path -LiteralPath $desktopPath -PathType Leaf)) {
    throw "VIPM Desktop executable not found beside the resolved CLI: '$desktopPath'."
}
$actualDesktop = (Get-Item -LiteralPath $desktopPath).VersionInfo.ProductVersion
if ($actualDesktop -ne $DesktopVersion) {
    throw "VIPM Desktop version mismatch: expected '$DesktopVersion', got '$actualDesktop'. Update CLI and Desktop together."
}
Write-Output "Verified VIPM CLI $CliVersion and Desktop $DesktopVersion."
