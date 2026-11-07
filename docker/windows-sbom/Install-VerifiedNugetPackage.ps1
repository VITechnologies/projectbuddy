<#
.SYNOPSIS
Verifies a NuGet package's SHA-512 against an expected value, then extracts it.

.DESCRIPTION
Generic, project-agnostic utility: downloads/callers supply any .nupkg
archive and its expected SHA-512 (base64); this refuses to extract on any
hash mismatch, then extracts via .NET's ZipFile (Expand-Archive rejects
the .nupkg extension).

Restored in VITechnologies/projectbuddy#76: removed in v0.3.24 alongside
the unrelated Install-SigningTools.ps1 (a real cleanup -- that script's
own SignTool/Azure-Artifact-Signing toolchain genuinely had zero
consumers). This script was swept up in the same removal because it was,
at the time, only ever called internally by Install-SigningTools.ps1 --
but VITechnologies/SandboxSBOMReporting's own CI
(.github/workflows/batch-pipeline.yml, "Run PSScriptAnalyzer" step) had
independently started calling this exact script directly, for a wholly
unrelated purpose (securely installing PSScriptAnalyzer from NuGet), a
fact missed at removal time. Nothing here is signing-specific; this is a
reusable, generic NuGet-verify-and-extract helper.
#>
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
