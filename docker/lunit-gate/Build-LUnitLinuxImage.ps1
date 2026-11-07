#requires -Version 5.1
<#
.SYNOPSIS
    Builds the ephemeral Linux LUnit release-gate image from a clean
    staging context (never the full worktree, so no .git or credentials
    leak into the build context).

.DESCRIPTION
    Invoked by .github/workflows/lunit-gate-linux.yml on an `ubuntu-latest`
    GitHub-hosted runner (per ADR-0003's explicit runner-class decision;
    PowerShell Core ships pre-installed on GitHub's ubuntu-latest image,
    so this script runs there directly -- no separate Linux-native build
    script needed).

    Unlike the Windows gate, deployment does not need an NSIS-built
    installer: confirmed via direct local Docker testing (see
    docs/lunit-gate/procedure.md's Phase 2 section) that Linux has
    neither a MAX_PATH limit nor a Windows-container-style SMB bind-mount
    boundary, so the two bugs that drove the Windows gate's NSIS
    migration simply do not apply here. The same
    Get-VerifiedLUnitPackageStaging.ps1 staging step is reused (it is
    already cross-platform), and the Dockerfile deploys the staged files
    with a plain `COPY`.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$Image,

    [string]$Network = 'default'
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$context = Join-Path ([IO.Path]::GetTempPath()) ('lunit-gate-linux-image-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $context -Force | Out-Null
try {
    $staging = Join-Path $context 'lunit-staging'
    & (Join-Path $PSScriptRoot 'Get-VerifiedLUnitPackageStaging.ps1') -StagingDirectory $staging
    if (-not (Test-Path -LiteralPath (Join-Path $staging 'dependency-manifest.json') -PathType Leaf)) {
        throw "LUnit package staging did not produce dependency-manifest.json."
    }

    Copy-Item (Join-Path $PSScriptRoot 'linux.Dockerfile') (Join-Path $context 'Dockerfile')
    Copy-Item (Join-Path $PSScriptRoot 'invoke_lunit_tests_linux.py') (Join-Path $context 'invoke_lunit_tests_linux.py')
    docker build --network $Network --tag $Image $context
    if ($LASTEXITCODE -ne 0) { throw "Required LUnit gate Linux image build failed: $LASTEXITCODE" }
}
finally {
    Remove-Item -LiteralPath $context -Recurse -Force -ErrorAction SilentlyContinue
}
