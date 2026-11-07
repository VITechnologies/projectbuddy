#requires -Version 5.1
<#
.SYNOPSIS
    Host-side staging step: downloads and SHA-256-verifies the LUnit/LUnit
    CLI LabVIEW VI packages, extracting each package's vi.lib\Astemes tree
    into a flat staging directory ready to be bundled into
    Install-LUnitPackages.nsi at compile time.

.DESCRIPTION
    Per operator direction (ADR-0003/REQ-034's implementation): deployment
    moved from an in-container PowerShell copy to an NSIS-built installer,
    a known-good, decades-proven mechanism for placing files into Windows
    system directories -- avoiding two real Windows-container-specific bugs
    hit during this engine's first implementation attempt (a Windows
    MAX_PATH violation during .NET ZipFile extraction, and an "access
    denied" failure moving a directory across a Windows-container's
    SMB-backed bind mount). Running entirely on the host (this script has
    no Windows-specific path assumptions and runs fine on Linux or
    Windows) means no network access or container-specific quirks are
    needed at container-build time at all -- the installer just bundles
    already-verified bytes.

    No VIPM install, no VIPM activation, no secrets -- matches
    ni/labview-icon-editor#575's own finding that VIPM installs stall in
    Windows containers, with one deliberate change per ADR-0003/REQ-034:
    package integrity is verified by a SHA-256 hash this engine computed
    and pinned itself (the upstream script only checks the publisher's
    MD5), not by re-trusting the publisher's own checksum algorithm for an
    ongoing, repeated release gate.

.PARAMETER StagingDirectory
    Where to write the "LUnit" and "LUnit CLI" folders (ready for the NSIS
    installer's `File /r` instructions) and dependency-manifest.json.
#>
[CmdletBinding()]
param(
    [string]$StagingDirectory
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# Versions from ni/labview-icon-editor#575's own pin. SHA-256 computed and
# verified by this engine from the publisher's own distribution (cross-checked
# against the publisher's published MD5 before being trusted) -- see
# docs/lunit-gate/procedure.md's Package provenance section for how these were
# obtained and how to refresh them at the next suite-pin update.
$script:LUnitPackages = @(
    [ordered]@{
        Name    = 'astemes_lib_lunit'
        Version = '2.0.13.6'
        Sha256  = '8C747D9FD57EF7FB21D3E3D9C3A59FED3D201843EF7D19DAAC54E3F564908D46'
        Folder  = 'LUnit'
    }
    [ordered]@{
        Name    = 'astemes_lib_lunit_cli'
        Version = '1.7.1.32'
        Sha256  = 'ADF8154D2C33C2AEDA4861B60D612DC2E69AE6D9C3672D58E89E154B96DF0F38'
        Folder  = 'LUnit CLI'
    }
)

function Confirm-PackageIntegrity {
    # Pure, offline-testable: verifies a downloaded (or any local) file is a
    # ZIP/VIP archive whose SHA-256 matches the pinned, trusted hash. No
    # network access -- separated from Get-LUnitPackageFile specifically so
    # this integrity check can be exercised directly by Pester fixtures
    # (scripts/tests/Get-VerifiedLUnitPackageStaging.Tests.ps1), not just
    # indirectly via a live download.
    param(
        [Parameter(Mandatory)] [string]$Path,
        [Parameter(Mandatory)] [string]$ExpectedSha256,
        [Parameter(Mandatory)] [string]$DisplayName
    )

    # [System.IO.File]::ReadAllBytes (not Get-Content -Encoding Byte /
    # -AsByteStream) deliberately, since those two parameter names are
    # mutually exclusive across Windows PowerShell 5.1 and PowerShell 7;
    # the raw .NET API behaves identically on both.
    $allBytes = [System.IO.File]::ReadAllBytes($Path)
    $header = $allBytes[0..([Math]::Min(3, $allBytes.Length - 1))]
    if ($header.Count -lt 4 -or $header[0] -ne 0x50 -or $header[1] -ne 0x4b) {
        throw "Downloaded package is not a VIP/ZIP archive: $DisplayName"
    }

    $hash = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
    if ($hash -ne $ExpectedSha256) {
        throw "SHA-256 mismatch for $DisplayName -- expected $ExpectedSha256, got $hash. Refusing to stage an unverified package."
    }
}

function Get-LUnitPackageFile {
    param(
        [Parameter(Mandatory)] $Package,
        [Parameter(Mandatory)] [string]$Directory
    )

    $filename = '{0}-{1}.vip' -f $Package.Name, $Package.Version
    $path = Join-Path $Directory $filename
    $url = "https://www.jkisoft.com/packages/packages/$($Package.Name)/$filename"
    Write-Host "Downloading required public package: $filename"
    Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $path -TimeoutSec 120
    Confirm-PackageIntegrity -Path $path -ExpectedSha256 $Package.Sha256 -DisplayName $filename
    $path
}

function New-LUnitPackageStaging {
    param(
        [Parameter(Mandatory)] [string]$StagingDirectory,
        [Parameter(Mandatory)] [string]$WorkDirectory
    )

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    New-Item -ItemType Directory -Path $WorkDirectory -Force | Out-Null
    New-Item -ItemType Directory -Path $StagingDirectory -Force | Out-Null
    foreach ($package in $script:LUnitPackages) {
        $file = Get-LUnitPackageFile -Package $package -Directory $WorkDirectory
        $extracted = Join-Path $WorkDirectory $package.Name
        if (Test-Path -LiteralPath $extracted) { Remove-Item -LiteralPath $extracted -Recurse -Force }
        [System.IO.Compression.ZipFile]::ExtractToDirectory($file, $extracted)

        $source = Join-Path $extracted "File Group 0/vi.lib/Astemes/$($package.Folder)"
        if (-not (Test-Path -LiteralPath $source -PathType Container)) {
            throw "Package $($package.Name) does not contain vi.lib/Astemes/$($package.Folder)."
        }
        $target = Join-Path $StagingDirectory $package.Folder
        if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }
        Copy-Item -LiteralPath $source -Destination $target -Recurse
        Write-Host "Staged $($package.Name) $($package.Version) to $target (SHA-256 verified)"
    }

    foreach ($relative in @('LUnit', 'LUnit CLI/LUnitCLI/RunOperation.vi')) {
        if (-not (Test-Path -LiteralPath (Join-Path $StagingDirectory $relative))) {
            throw "LUnit staging missing: $relative"
        }
    }
}

if ($MyInvocation.InvocationName -ne ".") {
    if (-not $StagingDirectory) {
        Write-Error "Pass -StagingDirectory."
        exit 1
    }

    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $work = Join-Path ([System.IO.Path]::GetTempPath()) ('lunit-gate-staging-work-' + [guid]::NewGuid().ToString('N'))
    try {
        New-LUnitPackageStaging -StagingDirectory $StagingDirectory -WorkDirectory $work
    }
    finally {
        if (Test-Path -LiteralPath $work) { Remove-Item -LiteralPath $work -Recurse -Force }
    }

    [ordered]@{
        installMethod = 'sha256-verified-nsis-installer'
        vipmUsed      = $false
        packages      = @($script:LUnitPackages | ForEach-Object {
            [ordered]@{ name = $_.Name; version = $_.Version; sha256 = $_.Sha256 }
        })
    } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $StagingDirectory 'dependency-manifest.json') -Encoding UTF8

    Write-Host 'LUnit package staging complete: no VIPM installation, activation, or secret used.'
}
