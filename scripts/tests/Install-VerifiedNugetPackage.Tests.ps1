#requires -Version 7.0
<#
Real, assertion-based Pester tests for Install-VerifiedNugetPackage.ps1 --
restored in VITechnologies/projectbuddy#76 after being incorrectly removed
in v0.3.24 based on a false "zero consumers" claim. Covers the success
path (correct hash, real extraction) and the failure path (hash mismatch
refuses extraction, destination never created).

Run with: pwsh -NoProfile -Command "Invoke-Pester -Path scripts/tests/Install-VerifiedNugetPackage.Tests.ps1 -CI"
#>

BeforeAll {
    $script:ScriptPath = Join-Path $PSScriptRoot ".." ".." "docker" "windows-sbom" "Install-VerifiedNugetPackage.ps1"

    function New-TestNugetPackage {
        param([string]$TempDir)
        $contentDir = Join-Path $TempDir "content"
        New-Item -ItemType Directory -Path $contentDir -Force | Out-Null
        Set-Content -LiteralPath (Join-Path $contentDir "hello.txt") -Value "hello from a real test fixture"
        $archivePath = Join-Path $TempDir "test.nupkg"
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [IO.Compression.ZipFile]::CreateFromDirectory($contentDir, $archivePath)
        $algorithm = [Security.Cryptography.SHA512]::Create()
        $stream = [IO.File]::OpenRead($archivePath)
        try { $hash = [Convert]::ToBase64String($algorithm.ComputeHash($stream)) }
        finally { $stream.Dispose(); $algorithm.Dispose() }
        return [PSCustomObject]@{ ArchivePath = $archivePath; Sha512 = $hash }
    }
}

Describe "Install-VerifiedNugetPackage.ps1" {
    BeforeEach {
        $script:TempDir = Join-Path ([System.IO.Path]::GetTempPath()) ("nuget-pkg-test-" + [guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Path $script:TempDir -Force | Out-Null
    }

    AfterEach {
        if (Test-Path -LiteralPath $script:TempDir) {
            Remove-Item -LiteralPath $script:TempDir -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    It "extracts a .nupkg whose real SHA-512 matches the expected value" {
        $package = New-TestNugetPackage -TempDir $script:TempDir
        $destination = Join-Path $script:TempDir "extracted"

        & $script:ScriptPath -ArchivePath $package.ArchivePath -ExpectedSha512 $package.Sha512 -Destination $destination

        Test-Path -LiteralPath (Join-Path $destination "hello.txt") | Should -BeTrue
    }

    It "throws and refuses to create the destination when the SHA-512 does not match" {
        $package = New-TestNugetPackage -TempDir $script:TempDir
        $destination = Join-Path $script:TempDir "should-not-exist"
        $wrongHash = [Convert]::ToBase64String([byte[]](, 0 * 64))

        { & $script:ScriptPath -ArchivePath $package.ArchivePath -ExpectedSha512 $wrongHash -Destination $destination } |
            Should -Throw "*SHA-512 mismatch*"

        Test-Path -LiteralPath $destination | Should -BeFalse
    }

    It "throws when the archive path does not exist" {
        $destination = Join-Path $script:TempDir "extracted"
        { & $script:ScriptPath -ArchivePath (Join-Path $script:TempDir "does-not-exist.nupkg") -ExpectedSha512 "AAAA" -Destination $destination } |
            Should -Throw "*archive is missing*"
    }

    It "throws when the destination already exists (refuses to overwrite)" {
        $package = New-TestNugetPackage -TempDir $script:TempDir
        $destination = Join-Path $script:TempDir "already-here"
        New-Item -ItemType Directory -Path $destination -Force | Out-Null

        { & $script:ScriptPath -ArchivePath $package.ArchivePath -ExpectedSha512 $package.Sha512 -Destination $destination } |
            Should -Throw "*already exists*"
    }
}
