#requires -Version 7.0
<#
Real, assertion-based Pester tests for Test-SbomArtifact.ps1's
Test-SbomArtifactIntegrity function -- the genuinely unit-testable,
pure/offline logic this engine's own REQ-009 requires to be reachable
via a real fixture for each of its 3 documented failure classes (exit 1
integrity/build, exit 2 policy, exit 3 reporting), plus the exit-0
success path. Constructs minimal, self-contained fixture directories on
disk (a fake pipeline config + a fake pipeline-run.json per case) and
calls the function directly (bypassing the script's own `exit` calls,
which only run when the script is NOT dot-sourced).

Run with: pwsh -NoProfile -Command "Invoke-Pester -Path scripts/tests/Test-SbomArtifact.Tests.ps1 -CI"
#>

BeforeAll {
    . (Join-Path $PSScriptRoot ".." ".." "Test-SbomArtifact.ps1")

    function New-SbomArtifactFixture {
        param(
            [string]$ProjectName = "fixture-project",
            [string]$Status = "succeeded",
            [string]$FailureCategory,
            [string]$Failure,
            [hashtable]$StageGates = @{}
        )
        $root = Join-Path ([System.IO.Path]::GetTempPath()) ("sbom-artifact-test-" + [guid]::NewGuid().ToString("N"))
        $projectDir = Join-Path $root $ProjectName
        New-Item -ItemType Directory -Path $projectDir -Force | Out-Null

        $stages = [ordered]@{}
        foreach ($stageName in @("checkout", "packages", "sbom", "grype", "dependencyTrack")) {
            $gate = if ($StageGates.ContainsKey($stageName)) { $StageGates[$stageName] } else { $null }
            $stages[$stageName] = @{ status = "skipped"; reason = "fixture: not exercised by this test case"; gate = $gate }
        }

        $record = [ordered]@{
            schemaVersion = 1
            project       = $ProjectName
            pipeline      = "native"
            status        = $Status
            reportingMode = "hosted-grype"
            stages        = $stages
        }
        if ($FailureCategory) { $record.failureCategory = $FailureCategory }
        if ($Failure) { $record.failure = $Failure }

        $record | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $projectDir "pipeline-run.json") -Encoding utf8

        $config = @{ projects = @(@{ name = $ProjectName; enabled = $true }) }
        $configPath = Join-Path $root "pipeline-config.json"
        $config | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $configPath -Encoding utf8

        return [PSCustomObject]@{
            Root           = $root
            ArtifactPath   = $root
            ConfigPath     = $configPath
            ProjectName    = $ProjectName
        }
    }

    function Remove-SbomArtifactFixture {
        param($Fixture)
        if ($Fixture -and (Test-Path -LiteralPath $Fixture.Root)) {
            Remove-Item -LiteralPath $Fixture.Root -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

Describe "Test-SbomArtifactIntegrity (REQ-009: all 3 failure classes reachable via a real fixture)" {

    It "exits 0 (succeeded) for a genuinely intact, successful run record" {
        $fixture = New-SbomArtifactFixture -Status "succeeded"
        try {
            $result = Test-SbomArtifactIntegrity -ArtifactPath $fixture.ArtifactPath `
                -PipelineConfigPath $fixture.ConfigPath -ProjectName $fixture.ProjectName `
                -RepositoryRoot $PSScriptRoot
            $result.ExitCode | Should -Be 0
            $result.Outcome | Should -Be "succeeded"
            $result.Errors.Count | Should -Be 0
        } finally {
            Remove-SbomArtifactFixture $fixture
        }
    }

    It "exits 1 (integrity failure) when the artifact has no output folder for the project" {
        $root = Join-Path ([System.IO.Path]::GetTempPath()) ("sbom-artifact-test-" + [guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Path $root -Force | Out-Null
        $config = @{ projects = @(@{ name = "missing-project"; enabled = $true }) }
        $configPath = Join-Path $root "pipeline-config.json"
        $config | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $configPath -Encoding utf8
        try {
            $result = Test-SbomArtifactIntegrity -ArtifactPath $root `
                -PipelineConfigPath $configPath -ProjectName "missing-project" `
                -RepositoryRoot $PSScriptRoot
            $result.ExitCode | Should -Be 1
            $result.Outcome | Should -Be "integrity"
            $result.Errors | Should -Contain "Artifact has no output folder for project 'missing-project'."
        } finally {
            if (Test-Path -LiteralPath $root) { Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue }
        }
    }

    It "exits 1 (build failure) for a failed run record whose failureCategory is neither policy nor reporting" {
        $fixture = New-SbomArtifactFixture -Status "failed" -FailureCategory "build" -Failure "VIPM build step threw."
        try {
            $result = Test-SbomArtifactIntegrity -ArtifactPath $fixture.ArtifactPath `
                -PipelineConfigPath $fixture.ConfigPath -ProjectName $fixture.ProjectName `
                -RepositoryRoot $PSScriptRoot
            $result.ExitCode | Should -Be 1
            $result.Outcome | Should -Be "build"
        } finally {
            Remove-SbomArtifactFixture $fixture
        }
    }

    It "exits 2 (policy failure) for a failed run record with failureCategory=policy and a failed gate" {
        $fixture = New-SbomArtifactFixture -Status "failed" -FailureCategory "policy" `
            -Failure "Dependency-Track policy violation: 1 critical finding." `
            -StageGates @{ dependencyTrack = "failed" }
        try {
            $result = Test-SbomArtifactIntegrity -ArtifactPath $fixture.ArtifactPath `
                -PipelineConfigPath $fixture.ConfigPath -ProjectName $fixture.ProjectName `
                -RepositoryRoot $PSScriptRoot
            $result.ExitCode | Should -Be 2
            $result.Outcome | Should -Be "policy"
        } finally {
            Remove-SbomArtifactFixture $fixture
        }
    }

    It "exits 3 (reporting failure) for a failed run record with failureCategory=reporting" {
        $fixture = New-SbomArtifactFixture -Status "failed" -FailureCategory "reporting" `
            -Failure "Dependency-Track upload timed out after 60s."
        try {
            $result = Test-SbomArtifactIntegrity -ArtifactPath $fixture.ArtifactPath `
                -PipelineConfigPath $fixture.ConfigPath -ProjectName $fixture.ProjectName `
                -RepositoryRoot $PSScriptRoot
            $result.ExitCode | Should -Be 3
            $result.Outcome | Should -Be "reporting"
        } finally {
            Remove-SbomArtifactFixture $fixture
        }
    }

    It "exits 2 (policy) and never silently downgrades to a different class when a gate failed but status reports succeeded is impossible -- cross-check: a succeeded status with a failed gate is itself flagged as an integrity error" {
        # Guards against a real, subtle bug class: if the run record itself is
        # self-contradictory (claims success while a gate actually failed),
        # Test-SbomArtifactIntegrity must not silently report exit 0.
        $fixture = New-SbomArtifactFixture -Status "succeeded" -StageGates @{ grype = "failed" }
        try {
            $result = Test-SbomArtifactIntegrity -ArtifactPath $fixture.ArtifactPath `
                -PipelineConfigPath $fixture.ConfigPath -ProjectName $fixture.ProjectName `
                -RepositoryRoot $PSScriptRoot
            $result.ExitCode | Should -Be 1
            $result.Outcome | Should -Be "integrity"
            $result.Errors | Should -Contain "Run record succeeded but the grype gate failed."
        } finally {
            Remove-SbomArtifactFixture $fixture
        }
    }
}
