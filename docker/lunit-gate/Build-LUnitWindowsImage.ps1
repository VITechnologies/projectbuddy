#requires -Version 5.1
<#
.SYNOPSIS
    Builds the ephemeral Windows LUnit release-gate image from a clean
    staging context (never the full worktree, so no .git or credentials
    leak into the build context).

.DESCRIPTION
    Adapted from ni/labview-icon-editor#575's Tooling/Build-LUnitWindowsImage.ps1.
    Invoked by .github/workflows/lunit-gate-windows.yml on a windows-2022
    GitHub-hosted runner (per ADR-0003's explicit runner-class decision;
    NSIS 3.10 ships pre-installed on that runner image).

    Deployment is NSIS-based (Install-LUnitPackages.nsi), not a raw
    PowerShell+.NET ZipFile copy: this script first runs
    Get-VerifiedLUnitPackageStaging.ps1 (SHA-256-verified download,
    producing a flat staging directory), then compiles the installer with
    those already-verified files bundled in via `makensis`, then builds
    the Docker image with the compiled .exe baked in. Validated end-to-end
    (real LabVIEW + LabVIEWCLI + the pinned ni/labview-icon-editor suite)
    against a local Vagrant harness
    (docker/lunit-gate/vagrant-nsis-test/) before being wired in here --
    see docs/lunit-gate/procedure.md.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$Image,

    [string]$Network = 'nat'
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$context = Join-Path ([IO.Path]::GetTempPath()) ('lunit-gate-image-' + [guid]::NewGuid().ToString('N'))
$staging = Join-Path ([IO.Path]::GetTempPath()) ('lunit-gate-staging-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $context -Force | Out-Null
try {
    # Not $LASTEXITCODE: that variable only reflects native/exe commands,
    # not a .ps1 invoked via `&` -- checking it here produced a false-
    # positive failure on a real live-dispatch run (staging had actually
    # succeeded; $LASTEXITCODE was stale/unset from this fresh shell,
    # and $null -ne 0 is true). Get-VerifiedLUnitPackageStaging.ps1 sets
    # $ErrorActionPreference = 'Stop' and throws on any real failure,
    # which propagates as a terminating exception (caught by this
    # script's own try/finally) without needing an exit-code check at
    # all; verify its actual output artifact instead.
    & (Join-Path $PSScriptRoot 'Get-VerifiedLUnitPackageStaging.ps1') -StagingDirectory $staging
    if (-not (Test-Path -LiteralPath (Join-Path $staging 'dependency-manifest.json') -PathType Leaf)) {
        throw "LUnit package staging did not produce dependency-manifest.json."
    }

    $installerExe = Join-Path $context 'Install-LUnitPackages.exe'
    & makensis -V2 "-DLUNIT_STAGING=$staging" "-DOUT_FILE=$installerExe" (Join-Path $PSScriptRoot 'Install-LUnitPackages.nsi')
    if ($LASTEXITCODE -ne 0) { throw "LUnit package installer compilation failed: $LASTEXITCODE" }
    if (-not (Test-Path -LiteralPath $installerExe -PathType Leaf)) { throw "makensis did not produce $installerExe." }

    Copy-Item (Join-Path $PSScriptRoot 'windows.Dockerfile') (Join-Path $context 'Dockerfile')
    docker build --network $Network --tag $Image $context
    if ($LASTEXITCODE -ne 0) { throw "Required LUnit gate image build failed: $LASTEXITCODE" }
}
finally {
    Remove-Item -LiteralPath $context -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
}

