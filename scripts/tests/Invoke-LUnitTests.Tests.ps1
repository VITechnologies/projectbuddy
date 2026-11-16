#requires -Version 7.0
<#
Real, assertion-based Pester tests for Invoke-LUnitTests.ps1's
Test-JUnitReportAcceptable function -- the pure, offline-testable JUnit
report/waiver-policy logic ADR-0003's round-2 Addendum and REQ-034 require
(a named, lapsing waiver for exactly one upstream test; every other
failure/error blocks the gate), plus the completeness/engine-commit checks
#94's independent post-implementation verification review added. Builds
small local JUnit XML fixtures, shaped like the real pinned suite's 10
test cases across 5 classes, and calls the function directly (bypassing
the script's own execution guard, which only runs when the script is NOT
dot-sourced).

Run with: pwsh -NoProfile -Command "Invoke-Pester -Path scripts/tests/Invoke-LUnitTests.Tests.ps1 -CI"
#>

BeforeAll {
    . (Join-Path $PSScriptRoot ".." ".." "docker" "lunit-gate" "Invoke-LUnitTests.ps1")

    $script:FixtureRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("lunit-tests-test-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $script:FixtureRoot -Force | Out-Null

    function New-JUnitFixture {
        param([string]$Path, [string]$Content)
        Set-Content -LiteralPath $Path -Value $Content -Encoding UTF8
    }

    # The pinned suite commit's real 10 test cases across 5 classes
    # (ni/labview-icon-editor @ d9eac771ccac0ed92bde81a031fe2f4ebe62c845),
    # reused verbatim so these fixtures match EXPECTED_TEST_COUNT and look
    # like the real lunit.xml (status= attributes included, per #94's
    # finding that the prior fixtures didn't resemble real LUnit output).
    function New-TenCaseJUnitXml {
        param(
            [string[]]$FailingNames = @(),
            [string[]]$ErroringNames = @(),
            [string[]]$SkippedNames = @(),
            [string]$FailureMessage = "boom"
        )
        $allNames = @(
            @{ Class = "Editor Position"; Name = "Test Position INI.vi" },
            @{ Class = "Editor Position"; Name = "Test Window Position.vi" },
            @{ Class = "History Tests"; Name = "Test Add Clears Redo.vi" },
            @{ Class = "History Tests"; Name = "Test Add and Undo All.vi" },
            @{ Class = "History Tests"; Name = "Test Overflow.vi" },
            @{ Class = "History Tests"; Name = "Test Undo Flag.vi" },
            @{ Class = "INI Settings"; Name = "Test INI Settings.vi" },
            @{ Class = "Library Icon Data"; Name = "Test Library Icon Tag.vi" },
            @{ Class = "Library Icon Data"; Name = "Test Write Read Library Icon Data.vi" },
            @{ Class = "Text-Based VI Icon Tests"; Name = "Test Split Text into Words.vi" }
        )
        $cases = foreach ($entry in $allNames) {
            if ($SkippedNames -contains $entry.Name) {
                "  <testcase classname=`"$($entry.Class)`" name=`"$($entry.Name)`" status=`"Skipped`"><skipped/></testcase>"
            }
            elseif ($FailingNames -contains $entry.Name) {
                "  <testcase classname=`"$($entry.Class)`" name=`"$($entry.Name)`" status=`"Failed`"><failure message=`"$FailureMessage`">detail</failure></testcase>"
            }
            elseif ($ErroringNames -contains $entry.Name) {
                "  <testcase classname=`"$($entry.Class)`" name=`"$($entry.Name)`" status=`"Failed`"><error message=`"$FailureMessage`">detail</error></testcase>"
            }
            else {
                "  <testcase classname=`"$($entry.Class)`" name=`"$($entry.Name)`" status=`"Passed`"/>"
            }
        }
        $failCount = $FailingNames.Count
        $errorCount = $ErroringNames.Count
        return "<?xml version=`"1.0`" encoding=`"UTF-8`"?>`n<testsuite name=`"Icon Editor LUnit`" tests=`"10`" failures=`"$failCount`" errors=`"$errorCount`">`n$($cases -join "`n")`n</testsuite>`n"
    }

    $script:AllPassXml = New-TenCaseJUnitXml
    $script:WaivedFailureXml = New-TenCaseJUnitXml -FailingNames @("Test Write Read Library Icon Data.vi") -FailureMessage "NI.Lib.VIIconTemplate expected versus NI_IconEditor saved"
    $script:BlockingFailureXml = New-TenCaseJUnitXml -FailingNames @("Test Add Clears Redo.vi") -FailureMessage "unexpected assertion failure"
    $script:BlockingErrorXml = New-TenCaseJUnitXml -ErroringNames @("Test INI Settings.vi") -FailureMessage "unhandled exception"
    # Per #94's review: the waiver list must still block an unwaived 4th
    # failure even when all named waivers are also present in the same
    # report (a mutant that only checked "any waived" rather than
    # "every failure is waived" would pass this).
    $script:ThreeWaiversPlusFourthFailureXml = New-TenCaseJUnitXml -FailingNames @("Test Write Read Library Icon Data.vi", "Test Add Clears Redo.vi") -FailureMessage "mixed waived and unwaived"
    $script:EmptySuiteXml = @'
<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="Icon Editor LUnit" tests="0" failures="0" errors="0">
</testsuite>
'@
    $script:TruncatedSuiteXml = @'
<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="Icon Editor LUnit" tests="1" failures="0" errors="0">
  <testcase classname="History Tests" name="Test Undo Flag.vi" status="Passed"/>
</testsuite>
'@
    $script:SkippedTestXml = New-TenCaseJUnitXml -SkippedNames @("Test Overflow.vi")
}

AfterAll {
    if (Test-Path -LiteralPath $script:FixtureRoot) {
        Remove-Item -LiteralPath $script:FixtureRoot -Recurse -Force
    }
}

Describe "Test-JUnitReportAcceptable" {
    It "passes when every test case is clean (zero failures, zero errors)" {
        $path = Join-Path $script:FixtureRoot "all-pass.xml"
        New-JUnitFixture -Path $path -Content $script:AllPassXml

        $result = Test-JUnitReportAcceptable -ReportPath $path -EngineCommit "f00dfeedf00dfeedf00dfeedf00dfeedf00dfeed"
        $result.outcome | Should -Be "PASS"
        $result.totalTests | Should -Be 10
        $result.waivedTests | Should -BeNullOrEmpty
    }

    It "records the engine commit distinctly from the suite commit" {
        $path = Join-Path $script:FixtureRoot "engine-commit.xml"
        New-JUnitFixture -Path $path -Content $script:AllPassXml

        $result = Test-JUnitReportAcceptable -ReportPath $path -EngineCommit "f00dfeedf00dfeedf00dfeedf00dfeedf00dfeed"
        $result.engineCommit | Should -Be "f00dfeedf00dfeedf00dfeedf00dfeedf00dfeed"
    }

    It "passes and records a waiver when only the named Test Write Read Library Icon Data.vi fails" {
        $path = Join-Path $script:FixtureRoot "waived.xml"
        New-JUnitFixture -Path $path -Content $script:WaivedFailureXml

        $result = Test-JUnitReportAcceptable -ReportPath $path -EngineCommit "f00dfeedf00dfeedf00dfeedf00dfeedf00dfeed"
        $result.outcome | Should -Be "PASS"
        $result.waivedTests | Should -Contain "Test Write Read Library Icon Data.vi"
    }

    It "throws (blocks the gate) when any OTHER test fails" {
        $path = Join-Path $script:FixtureRoot "blocking-failure.xml"
        New-JUnitFixture -Path $path -Content $script:BlockingFailureXml

        { Test-JUnitReportAcceptable -ReportPath $path -EngineCommit "f00dfeedf00dfeedf00dfeedf00dfeedf00dfeed" } | Should -Throw -ExpectedMessage "*Test Add Clears Redo.vi*"
    }

    It "throws (blocks the gate) when any OTHER test errors, not just fails" {
        $path = Join-Path $script:FixtureRoot "blocking-error.xml"
        New-JUnitFixture -Path $path -Content $script:BlockingErrorXml

        { Test-JUnitReportAcceptable -ReportPath $path -EngineCommit "f00dfeedf00dfeedf00dfeedf00dfeedf00dfeed" } | Should -Throw -ExpectedMessage "*Test INI Settings.vi*"
    }

    It "throws (blocks the gate) on an unwaived failure even when the named waiver is also present in the same report" {
        $path = Join-Path $script:FixtureRoot "three-waivers-plus-fourth.xml"
        New-JUnitFixture -Path $path -Content $script:ThreeWaiversPlusFourthFailureXml

        { Test-JUnitReportAcceptable -ReportPath $path -EngineCommit "f00dfeedf00dfeedf00dfeedf00dfeedf00dfeed" } | Should -Throw -ExpectedMessage "*Test Add Clears Redo.vi*"
    }

    It "throws when the report has zero test cases (the pinned suite did not run)" {
        $path = Join-Path $script:FixtureRoot "empty.xml"
        New-JUnitFixture -Path $path -Content $script:EmptySuiteXml

        { Test-JUnitReportAcceptable -ReportPath $path -EngineCommit "f00dfeedf00dfeedf00dfeedf00dfeedf00dfeed" } | Should -Throw -ExpectedMessage "*zero test cases*"
    }

    It "throws when the report is missing test cases (a truncated/partial run must not pass)" {
        $path = Join-Path $script:FixtureRoot "truncated.xml"
        New-JUnitFixture -Path $path -Content $script:TruncatedSuiteXml

        { Test-JUnitReportAcceptable -ReportPath $path -EngineCommit "f00dfeedf00dfeedf00dfeedf00dfeedf00dfeed" } | Should -Throw -ExpectedMessage "*expected exactly 10*"
    }

    It "throws (blocks the gate) when a test case is skipped, even though it isn't a failure/error" {
        $path = Join-Path $script:FixtureRoot "skipped.xml"
        New-JUnitFixture -Path $path -Content $script:SkippedTestXml

        { Test-JUnitReportAcceptable -ReportPath $path -EngineCommit "f00dfeedf00dfeedf00dfeedf00dfeedf00dfeed" } | Should -Throw -ExpectedMessage "*Test Overflow.vi*"
    }

    It "throws when no report file exists at all" {
        $path = Join-Path $script:FixtureRoot "does-not-exist.xml"

        { Test-JUnitReportAcceptable -ReportPath $path -EngineCommit "f00dfeedf00dfeedf00dfeedf00dfeedf00dfeed" } | Should -Throw -ExpectedMessage "*No JUnit report*"
    }
}

Describe "Assert-CommitSha" {
    It "accepts a full 40-character lowercase-hex commit SHA" {
        { Assert-CommitSha -Value "d9eac771ccac0ed92bde81a031fe2f4ebe62c845" -Label "-SuiteCommit" } | Should -Not -Throw
    }

    It "rejects a mutable ref such as a branch name" {
        { Assert-CommitSha -Value "main" -Label "-SuiteCommit" } | Should -Throw -ExpectedMessage "*-SuiteCommit*"
    }

    It "rejects an uppercase or short SHA" {
        { Assert-CommitSha -Value "D9EAC77" -Label "-SuiteCommit" } | Should -Throw
    }
}
