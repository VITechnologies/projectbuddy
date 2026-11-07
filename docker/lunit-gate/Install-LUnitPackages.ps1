#requires -Version 5.1
<#
.SYNOPSIS
    Deploys the LUnit/LUnit CLI LabVIEW VI packages by direct, SHA-256-verified
    file copy -- no VIPM install, no VIPM activation, no secrets. Run once,
    during the Windows LUnit container image build (see windows.Dockerfile).

.DESCRIPTION
    Adapted from ni/labview-icon-editor#575's Tooling/Run-LUnitWindowsContainer.ps1
    'Setup' stage (VIPM installs are reported to stall in Windows containers,
    so the package files are extracted and copied directly instead), with one
    deliberate change per ADR-0003/REQ-034: package integrity is verified by a
    SHA-256 hash this engine computed and pinned itself (the upstream script
    only checks the publisher's MD5), not by re-trusting the publisher's own
    checksum algorithm for an ongoing, repeated release gate.

    Only each package's vi.lib\Astemes tree is copied; the packages' own
    post-install actions (menus, global CLI registration) are skipped, which
    is why Invoke-LUnitTests.ps1 passes -AdditionalOperationDirectory to
    LabVIEWCLI instead of relying on a registered CLI operation.

.PARAMETER LabviewRoot
    The LabVIEW installation directory inside the container
    (e.g. 'C:\Program Files\National Instruments\LabVIEW 2026').

.PARAMETER ResultsDirectory
    Where to write dependency-manifest.json (evidence of what was deployed).
#>
[CmdletBinding()]
param(
    [string]$LabviewRoot,
    [string]$ResultsDirectory
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

function Confirm-PackageIntegrity {
    # Pure, offline-testable: verifies a downloaded (or any local) file is a
    # ZIP/VIP archive whose SHA-256 matches the pinned, trusted hash. No
    # network access -- separated from Get-LUnitPackageFile specifically so
    # this integrity check can be exercised directly by Pester fixtures
    # (scripts/tests/Install-LUnitPackages.Tests.ps1), not just indirectly
    # via a live download.
    param(
        [Parameter(Mandatory)] [string]$Path,
        [Parameter(Mandatory)] [string]$ExpectedSha256,
        [Parameter(Mandatory)] [string]$DisplayName
    )

    # [System.IO.File]::ReadAllBytes (not Get-Content -Encoding Byte / -AsByteStream)
    # deliberately, since those two parameter names are mutually exclusive across
    # Windows PowerShell 5.1 (this container's runtime -- see windows.Dockerfile's
    # plain `powershell` SHELL) and PowerShell 7 (this repo's Pester test runtime);
    # the raw .NET API behaves identically on both.
    $allBytes = [System.IO.File]::ReadAllBytes($Path)
    $header = $allBytes[0..([Math]::Min(3, $allBytes.Length - 1))]
    if ($header.Count -lt 4 -or $header[0] -ne 0x50 -or $header[1] -ne 0x4b) {
        throw "Downloaded package is not a VIP/ZIP archive: $DisplayName"
    }

    $hash = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
    if ($hash -ne $ExpectedSha256) {
        throw "SHA-256 mismatch for $DisplayName -- expected $ExpectedSha256, got $hash. Refusing to deploy an unverified package."
    }
}

function Install-LUnitPackageFiles {
    param(
        [Parameter(Mandatory)] [string]$Destination,
        [Parameter(Mandatory)] [string]$WorkDirectory
    )

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    New-Item -ItemType Directory -Path $WorkDirectory -Force | Out-Null
    foreach ($package in $script:LUnitPackages) {
        $file = Get-LUnitPackageFile -Package $package -Directory $WorkDirectory
        $extracted = Join-Path $WorkDirectory $package.Name
        if (Test-Path -LiteralPath $extracted) { Remove-Item -LiteralPath $extracted -Recurse -Force }
        [System.IO.Compression.ZipFile]::ExtractToDirectory($file, $extracted)

        $source = Join-Path $extracted "File Group 0\vi.lib\Astemes\$($package.Folder)"
        if (-not (Test-Path -LiteralPath $source -PathType Container)) {
            throw "Package $($package.Name) does not contain vi.lib\Astemes\$($package.Folder)."
        }
        $target = Join-Path $Destination "vi.lib\Astemes\$($package.Folder)"
        if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }
        New-Item -ItemType Directory -Path (Split-Path $target) -Force | Out-Null
        Copy-Item -LiteralPath $source -Destination $target -Recurse
        Write-Host "Deployed $($package.Name) $($package.Version) to $target (SHA-256 verified)"
    }

    foreach ($relative in @('vi.lib\Astemes\LUnit', 'vi.lib\Astemes\LUnit CLI\LUnitCLI\RunOperation.vi')) {
        if (-not (Test-Path -LiteralPath (Join-Path $Destination $relative))) {
            throw "LUnit deployment missing: $relative"
        }
    }
}

function Set-LabviewTcpServerSettings {
    param([Parameter(Mandatory)] [string]$LabviewRoot)

    $ini = Join-Path $LabviewRoot 'LabVIEW.ini'
    $lines = @(Get-Content -LiteralPath $ini)
    foreach ($setting in @('server.tcp.enabled=True', 'server.tcp.port=3363', 'server.tcp.access=+127.0.0.1', 'server.vi.access=+*')) {
        $key = $setting.Split('=')[0]
        $lines = @($lines | Where-Object { $_ -notmatch ('^\s*' + [regex]::Escape($key) + '\s*=') })
        $lines += $setting
    }
    $lines | Set-Content -LiteralPath $ini -Encoding Ascii
}

if ($MyInvocation.InvocationName -ne ".") {
    if (-not $LabviewRoot -or -not $ResultsDirectory) {
        Write-Error "Pass -LabviewRoot and -ResultsDirectory."
        exit 1
    }

    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    New-Item -ItemType Directory -Path $ResultsDirectory -Force | Out-Null
    Set-LabviewTcpServerSettings -LabviewRoot $LabviewRoot

    $work = Join-Path $env:TEMP 'lunit-gate-packages'
    try {
        Install-LUnitPackageFiles -Destination $LabviewRoot -WorkDirectory $work
    }
    finally {
        if (Test-Path -LiteralPath $work) { Remove-Item -LiteralPath $work -Recurse -Force }
    }

    $labviewExe = Join-Path $LabviewRoot 'LabVIEW.exe'
    [ordered]@{
        labviewVersion = if (Test-Path -LiteralPath $labviewExe) { (Get-Item -LiteralPath $labviewExe).VersionInfo.FileVersion } else { $null }
        installMethod  = 'sha256-verified-vip-file-copy'
        frameworkPath  = Join-Path $LabviewRoot 'vi.lib\Astemes\LUnit'
        operationPath  = Join-Path $LabviewRoot 'vi.lib\Astemes\LUnit CLI\LUnitCLI'
        vipmUsed       = $false
        packages       = @($script:LUnitPackages | ForEach-Object {
            [ordered]@{ name = $_.Name; version = $_.Version; sha256 = $_.Sha256 }
        })
    } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $ResultsDirectory 'dependency-manifest.json') -Encoding UTF8

    Write-Host 'LUnit package deployment complete: no VIPM installation, activation, or secret used.'
}

