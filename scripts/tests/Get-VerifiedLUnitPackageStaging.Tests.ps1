#requires -Version 7.0
<#
Real, assertion-based Pester tests for Get-VerifiedLUnitPackageStaging.ps1's
Confirm-PackageIntegrity function -- the pure, offline-testable integrity
check this engine's REQ-034 requires (SHA-256, not MD5, with no network
access needed to test the check itself). Builds small local fixture files
and calls the function directly (bypassing the script's own execution
guard, which only runs when the script is NOT dot-sourced).

Note: this script now only STAGES verified package files for later NSIS
compilation (Install-LUnitPackages.nsi) -- it no longer deploys directly
into a LabVIEW installation (see docker/lunit-gate/windows.Dockerfile and
docs/lunit-gate/procedure.md for why: NSIS's own File/CreateDirectory
instructions replaced a raw PowerShell+.NET ZipFile approach after two
real Windows-container-specific failures).

Run with: pwsh -NoProfile -Command "Invoke-Pester -Path scripts/tests/Get-VerifiedLUnitPackageStaging.Tests.ps1 -CI"
#>

BeforeAll {
    . (Join-Path $PSScriptRoot ".." ".." "docker" "lunit-gate" "Get-VerifiedLUnitPackageStaging.ps1")

    $script:FixtureRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("lunit-staging-test-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $script:FixtureRoot -Force | Out-Null

    function New-ZipFixture {
        param([string]$Path, [string]$Content = "fixture")
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        if (Test-Path -LiteralPath $Path) { Remove-Item -LiteralPath $Path -Force }
        $tempDir = Join-Path $script:FixtureRoot ("inner-" + [guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Path $tempDir -Force | Out-Null
        Set-Content -LiteralPath (Join-Path $tempDir "payload.txt") -Value $Content
        [System.IO.Compression.ZipFile]::CreateFromDirectory($tempDir, $Path)
    }

    function New-NonZipFixture {
        param([string]$Path)
        Set-Content -LiteralPath $Path -Value "not a zip archive"
    }
}

AfterAll {
    if (Test-Path -LiteralPath $script:FixtureRoot) {
        Remove-Item -LiteralPath $script:FixtureRoot -Recurse -Force
    }
}

Describe "Confirm-PackageIntegrity" {
    It "accepts a ZIP/VIP file whose SHA-256 matches the expected hash" {
        $path = Join-Path $script:FixtureRoot "good.vip"
        New-ZipFixture -Path $path -Content "known-good-content"
        $expected = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash

        { Confirm-PackageIntegrity -Path $path -ExpectedSha256 $expected -DisplayName "good.vip" } | Should -Not -Throw
    }

    It "throws on a SHA-256 mismatch, refusing to stage an unverified package" {
        $path = Join-Path $script:FixtureRoot "tampered.vip"
        New-ZipFixture -Path $path -Content "tampered-content"

        { Confirm-PackageIntegrity -Path $path -ExpectedSha256 ("0" * 64) -DisplayName "tampered.vip" } |
            Should -Throw -ExpectedMessage "*SHA-256 mismatch*"
    }

    It "throws when the file is not a ZIP/VIP archive, even if the hash matches" {
        $path = Join-Path $script:FixtureRoot "notazip.vip"
        New-NonZipFixture -Path $path
        $actualHash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash

        { Confirm-PackageIntegrity -Path $path -ExpectedSha256 $actualHash -DisplayName "notazip.vip" } |
            Should -Throw -ExpectedMessage "*not a VIP/ZIP archive*"
    }

    It "pins a real SHA-256 (64 hex chars) for astemes_lib_lunit, not the publisher's MD5" {
        $package = $script:LUnitPackages | Where-Object { $_.Name -eq "astemes_lib_lunit" }
        $package | Should -Not -BeNullOrEmpty
        $package.Sha256 | Should -Match "^[0-9A-F]{64}$"
    }

    It "pins a real SHA-256 (64 hex chars) for astemes_lib_lunit_cli, not the publisher's MD5" {
        $package = $script:LUnitPackages | Where-Object { $_.Name -eq "astemes_lib_lunit_cli" }
        $package | Should -Not -BeNullOrEmpty
        $package.Sha256 | Should -Match "^[0-9A-F]{64}$"
    }
}
