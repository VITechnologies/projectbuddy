[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$ArchivePath,

    [Parameter(Mandatory)]
    [string]$ExpectedSha512,

    [Parameter(Mandatory)]
    [string]$Destination
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path -LiteralPath $ArchivePath -PathType Leaf)) { throw "NuGet package archive is missing." }
if (Test-Path -LiteralPath $Destination) { throw "NuGet package destination already exists: '$Destination'." }

try { $expectedBytes = [Convert]::FromBase64String($ExpectedSha512) }
catch { throw "Expected NuGet SHA-512 is not valid base64." }
if ($expectedBytes.Length -ne 64) { throw "Expected NuGet SHA-512 must be 64 bytes." }

$algorithm = [Security.Cryptography.SHA512]::Create()
$stream = [IO.File]::OpenRead($ArchivePath)
try { $actualHash = [Convert]::ToBase64String($algorithm.ComputeHash($stream)) }
finally {
    $stream.Dispose()
    $algorithm.Dispose()
}
if ($actualHash -cne $ExpectedSha512) { throw "NuGet package SHA-512 mismatch; refusing extraction." }

$null = New-Item -ItemType Directory -Path $Destination
# Expand-Archive rejects the .nupkg extension.
Add-Type -AssemblyName System.IO.Compression.FileSystem
[IO.Compression.ZipFile]::ExtractToDirectory($ArchivePath, $Destination)