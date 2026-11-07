#requires -Version 7.0
<#
Real, assertion-based Pester tests for Invoke-LUnitTests.ps1's
Test-JUnitReportAcceptable function -- the pure, offline-testable JUnit
report/waiver-policy logic ADR-0003's round-2 Addendum and REQ-034 require
(a named, lapsing waiver for exactly one upstream test; every other
failure/error blocks the gate). Builds small local JUnit XML fixtures and
calls the function directly (bypassing the script's own execution guard,
which only runs when the script is NOT dot-sourced).

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

    $script:AllPassXml = @'
<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="Icon Editor LUnit" tests="3" failures="0" errors="0">
  <testcase classname="History Tests" name="Test Undo.vi" />
  <testcase classname="Editor Position" name="Test Save Position.vi" />
  <testcase classname="INI Settings" name="Test Read Setting.vi" />
</testsuite>
'@

    $script:WaivedFailureXml = @'
<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="Icon Editor LUnit" tests="2" failures="1" errors="0">
  <testcase classname="Library Icon Data" name="Test Write Read Library Icon Data.vi">
    <failure message="NI.Lib.VIIconTemplate expected versus NI_IconEditor saved">tag mismatch</failure>
  </testcase>
  <testcase classname="History Tests" name="Test Undo.vi" />
</testsuite>
'@

    $script:BlockingFailureXml = @'
<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="Icon Editor LUnit" tests="2" failures="1" errors="0">
  <testcase classname="History Tests" name="Test Undo.vi">
    <failure message="unexpected assertion failure">boom</failure>
  </testcase>
  <testcase classname="Editor Position" name="Test Save Position.vi" />
</testsuite>
'@

    $script:BlockingErrorXml = @'
<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="Icon Editor LUnit" tests="1" failures="0" errors="1">
  <testcase classname="INI Settings" name="Test Read Setting.vi">
    <error message="unhandled exception">crashed</error>
  </testcase>
</testsuite>
'@

    $script:EmptySuiteXml = @'
<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="Icon Editor LUnit" tests="0" failures="0" errors="0">
</testsuite>
'@
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

        $result = Test-JUnitReportAcceptable -ReportPath $path
        $result.outcome | Should -Be "PASS"
        $result.totalTests | Should -Be 3
        $result.waivedTests | Should -BeNullOrEmpty
    }

    It "passes and records a waiver when only the named Test Write Read Library Icon Data.vi fails" {
        $path = Join-Path $script:FixtureRoot "waived.xml"
        New-JUnitFixture -Path $path -Content $script:WaivedFailureXml

        $result = Test-JUnitReportAcceptable -ReportPath $path
        $result.outcome | Should -Be "PASS"
        $result.waivedTests | Should -Contain "Test Write Read Library Icon Data.vi"
    }

    It "throws (blocks the gate) when any OTHER test fails" {
        $path = Join-Path $script:FixtureRoot "blocking-failure.xml"
        New-JUnitFixture -Path $path -Content $script:BlockingFailureXml

        { Test-JUnitReportAcceptable -ReportPath $path } | Should -Throw -ExpectedMessage "*Test Undo.vi*"
    }

    It "throws (blocks the gate) when any OTHER test errors, not just fails" {
        $path = Join-Path $script:FixtureRoot "blocking-error.xml"
        New-JUnitFixture -Path $path -Content $script:BlockingErrorXml

        { Test-JUnitReportAcceptable -ReportPath $path } | Should -Throw -ExpectedMessage "*Test Read Setting.vi*"
    }

    It "throws when the report has zero test cases (the pinned suite did not run)" {
        $path = Join-Path $script:FixtureRoot "empty.xml"
        New-JUnitFixture -Path $path -Content $script:EmptySuiteXml

        { Test-JUnitReportAcceptable -ReportPath $path } | Should -Throw -ExpectedMessage "*zero test cases*"
    }

    It "throws when no report file exists at all" {
        $path = Join-Path $script:FixtureRoot "does-not-exist.xml"

        { Test-JUnitReportAcceptable -ReportPath $path } | Should -Throw -ExpectedMessage "*No JUnit report*"
    }
}
