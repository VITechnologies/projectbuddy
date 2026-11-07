#requires -Version 5.1
<#
.SYNOPSIS
    Builds the ephemeral Windows LUnit release-gate image from a clean
    staging context (never the full worktree, so no .git or credentials
    leak into the build context).

.DESCRIPTION
    Adapted from ni/labview-icon-editor#575's Tooling/Build-LUnitWindowsImage.ps1.
    Invoked by .github/workflows/lunit-gate-windows.yml on a windows-2022
    GitHub-hosted runner (per ADR-0003's explicit runner-class decision).
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
New-Item -ItemType Directory -Path $context -Force | Out-Null
try {
    Copy-Item (Join-Path $PSScriptRoot 'Install-LUnitPackages.ps1') $context
    Copy-Item (Join-Path $PSScriptRoot 'windows.Dockerfile') (Join-Path $context 'Dockerfile')
    docker build --network $Network --tag $Image $context
    if ($LASTEXITCODE -ne 0) { throw "Required LUnit gate image build failed: $LASTEXITCODE" }
}
finally {
    Remove-Item -LiteralPath $context -Recurse -Force
}
