[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$Version,

    [Parameter(Mandatory)]
    [string]$Sha256,

    [Parameter(Mandatory)]
    [string]$InstallDirectory,

    [string]$ArchivePath
)

$ErrorActionPreference = "Stop"
if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw "Grype version must be an exact semantic version." }
if ($Sha256 -notmatch '^[0-9a-f]{64}$') { throw "Grype SHA-256 must be 64 lowercase hexadecimal characters." }

$temporaryDirectory = Join-Path ([IO.Path]::GetTempPath()) ("grype-install-" + [guid]::NewGuid().ToString("N"))
$null = New-Item -ItemType Directory -Path $temporaryDirectory
try {
    $archive = Join-Path $temporaryDirectory "grype.zip"
    if ($ArchivePath) {
        if (-not (Test-Path -LiteralPath $ArchivePath -PathType Leaf)) { throw "Grype archive is missing." }
        Copy-Item -LiteralPath $ArchivePath -Destination $archive
    } else {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $uri = "https://github.com/anchore/grype/releases/download/v$Version/grype_${Version}_windows_amd64.zip"
        Invoke-WebRequest -UseBasicParsing -Uri $uri -OutFile $archive
    }

    $actualSha256 = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualSha256 -ne $Sha256) { throw "Grype archive SHA-256 mismatch; refusing extraction and installation." }

    $extractionDirectory = Join-Path $temporaryDirectory "extracted"
    Expand-Archive -LiteralPath $archive -DestinationPath $extractionDirectory
    $binary = Join-Path $extractionDirectory "grype.exe"
    if (-not (Test-Path -LiteralPath $binary -PathType Leaf)) { throw "Verified Grype archive has no grype.exe." }
    New-Item -ItemType Directory -Path $InstallDirectory -Force | Out-Null
    Copy-Item -LiteralPath $binary -Destination (Join-Path $InstallDirectory "grype.exe") -Force
} finally {
    if (Test-Path -LiteralPath $temporaryDirectory) {
        Remove-Item -LiteralPath $temporaryDirectory -Recurse -Force
    }
}