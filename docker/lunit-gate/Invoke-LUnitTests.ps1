#requires -Version 5.1
<#
.SYNOPSIS
    Clones a pinned ni/labview-icon-editor commit and runs its real LUnit
    test suite via native LabVIEWCLI, non-interactively, inside the Windows
    LUnit container built from windows.Dockerfile.

.DESCRIPTION
    Adapted from ni/labview-icon-editor#575's Tooling/Run-LUnitWindowsContainer.ps1
    'Test' stage. Per ADR-0003/REQ-034: runs the suite unattended against a
    pinned external commit (not this engine's own code -- this gate proves
    this engine's LabVIEW container environment can execute a real,
    independently-maintained LabVIEW project's automated tests), with no
    VIPM installation, activation, or secret anywhere in this process.

.PARAMETER SuiteCommit
    The pinned ni/labview-icon-editor commit SHA to clone and test against.
    Changing this is a deliberate pin bump (see docs/lunit-gate/procedure.md),
    not a default to drift silently.

.PARAMETER ResultsDirectory
    Where lunit.xml and supporting logs are written.
#>
[CmdletBinding()]
param(
    [string]$SuiteCommit,
    [string]$ResultsDirectory
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# Per ADR-0003's round-2 Addendum: this is the ONLY permitted waiver, re-approved
# at every suite-pin update or it lapses. Any other failing/erroring test blocks
# the gate; a different waiver requires a new superseding ADR, not an edit here.
$script:AllowedWaiverTestName = 'Test Write Read Library Icon Data.vi'

$script:labviewProcess = $null

function Start-TestLabVIEW {
    param([Parameter(Mandatory)] [string]$Project)

    $process = Start-Process -FilePath $labview -ArgumentList @('--headless', "`"$Project`"") -PassThru `
        -RedirectStandardOutput (Join-Path $ResultsDirectory 'labview-test.log') `
        -RedirectStandardError (Join-Path $ResultsDirectory 'labview-test.stderr.log')
    $script:labviewProcess = $process
    $deadline = (Get-Date).AddMinutes(3)
    while ((Get-Date) -lt $deadline) {
        if ($process.HasExited) {
            throw "LabVIEW exited with code $($process.ExitCode) before the VI Server became ready. See labview-test logs."
        }
        $client = [System.Net.Sockets.TcpClient]::new()
        try {
            $connection = $client.ConnectAsync('127.0.0.1', 3363)
            if ($connection.Wait(1000) -and $client.Connected) {
                return
            }
        }
        catch [System.AggregateException] {
            if ($_.Exception.GetBaseException() -isnot [System.Net.Sockets.SocketException]) { throw }
        }
        finally {
            $client.Dispose()
        }
        Start-Sleep -Seconds 2
    }
    throw 'LabVIEW VI Server did not become ready on port 3363 within 3 minutes.'
}

function Stop-TestLabVIEW {
    if ($script:labviewProcess -and -not $script:labviewProcess.HasExited) {
        Stop-Process -Id $script:labviewProcess.Id -Force
        $script:labviewProcess.WaitForExit()
    }
    $script:labviewProcess = $null
}

function Assert-NoVipmPresence {
    # REQ-034: "no VIPM installation, activation call, secret, or credential
    # present anywhere in the container's build or runtime" -- checked, not
    # merely assumed.
    $vipmMarkers = @(
        'C:\Program Files (x86)\National Instruments\VI Package Manager',
        'C:\ProgramData\JKI\VIPM'
    )
    foreach ($marker in $vipmMarkers) {
        if (Test-Path -LiteralPath $marker) {
            throw "VIPM presence detected at $marker -- REQ-034 requires no VIPM installation anywhere in this container."
        }
    }
    foreach ($name in @('VIPM_SERIAL_NUMBER', 'VIPM_FULL_NAME', 'VIPM_EMAIL')) {
        if ([Environment]::GetEnvironmentVariable($name)) {
            throw "$name is set in this container's environment -- REQ-034 requires no VIPM-related secret anywhere."
        }
    }
}

function Get-PinnedSuiteArchive {
    # No `git` dependency: downloads GitHub's own commit-archive zip (an
    # exact-SHA archive is inherently that commit; GitHub's codeload service
    # only serves what actually exists at that SHA, so a successful download
    # and extraction is itself the provenance check -- no separate
    # `git rev-parse` verification is meaningful or necessary here).
    #
    # Deliberately short temp-path components (a short token, not "$Repo-$Commit"
    # repeated): GitHub's archive always names its own top-level folder
    # "$Repo-$Commit" (unavoidable, upstream's own convention, ~60+ chars for a
    # full SHA), and this suite's own deep paths (e.g.
    # "Test\Manual Tests\Reference Files\<long filename>.md") add more on top --
    # a first attempt that also repeated "$Repo-$Commit" in the *extraction
    # root* hit Windows' legacy 260-char MAX_PATH during ExtractToDirectory.
    param(
        [Parameter(Mandatory)] [string]$Owner,
        [Parameter(Mandatory)] [string]$Repo,
        [Parameter(Mandatory)] [string]$Commit,
        [Parameter(Mandatory)] [string]$Destination
    )

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $shortToken = [guid]::NewGuid().ToString('N').Substring(0, 8)
    $zipPath = Join-Path ([System.IO.Path]::GetTempPath()) "lg-$shortToken.zip"
    $extractRoot = Join-Path ([System.IO.Path]::GetTempPath()) "lg-$shortToken"
    try {
        $url = "https://codeload.github.com/$Owner/$Repo/zip/$Commit"
        Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $zipPath -TimeoutSec 180
        if (Test-Path -LiteralPath $extractRoot) { Remove-Item -LiteralPath $extractRoot -Recurse -Force }
        [System.IO.Compression.ZipFile]::ExtractToDirectory($zipPath, $extractRoot)

        $extractedFolder = Get-ChildItem -LiteralPath $extractRoot -Directory | Select-Object -First 1
        if (-not $extractedFolder) {
            throw "GitHub archive for $Owner/$Repo@$Commit extracted no top-level folder."
        }
        if (Test-Path -LiteralPath $Destination) { Remove-Item -LiteralPath $Destination -Recurse -Force }
        Move-Item -LiteralPath $extractedFolder.FullName -Destination $Destination
    }
    finally {
        Remove-Item -LiteralPath $zipPath -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $extractRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Test-JUnitReportAcceptable {
    param([Parameter(Mandatory)] [string]$ReportPath)

    if (-not (Test-Path -LiteralPath $ReportPath -PathType Leaf)) {
        throw "No JUnit report was produced at $ReportPath."
    }

    [xml]$report = Get-Content -LiteralPath $ReportPath -Raw
    $cases = @($report.SelectNodes('//testcase'))
    if ($cases.Count -eq 0) {
        throw 'JUnit report contains zero test cases -- the pinned suite did not run.'
    }

    $blocking = [System.Collections.Generic.List[string]]::new()
    $waived = [System.Collections.Generic.List[string]]::new()
    foreach ($case in $cases) {
        # Explicit if/else (not the ?? operator): this script runs under
        # Windows PowerShell 5.1 in production (invoked as powershell.exe,
        # not pwsh.exe -- see lunit-gate-windows.yml), which predates ??.
        $problem = $case.SelectSingleNode('failure')
        if (-not $problem) { $problem = $case.SelectSingleNode('error') }
        if (-not $problem) { continue }
        $name = $case.GetAttribute('name')
        if ($name -eq $script:AllowedWaiverTestName) {
            $waived.Add($name)
            Write-Warning "Known, named waiver (per ADR-0003): '$name' failed/errored and is being waived, not treated as blocking."
            continue
        }
        $blocking.Add("$($case.GetAttribute('classname')) :: $name -- $($problem.GetAttribute('message'))")
    }

    if ($blocking.Count -gt 0) {
        throw "LUnit gate FAIL: $($blocking.Count) test case(s) failed/errored without a named waiver:`n$($blocking -join "`n")"
    }

    [ordered]@{
        outcome       = 'PASS'
        totalTests    = $cases.Count
        waivedTests   = @($waived)
        suiteCommit   = $SuiteCommit
        vipmPresence  = $false
    }
}

if ($MyInvocation.InvocationName -ne ".") {
    if (-not $SuiteCommit -or -not $ResultsDirectory) {
        Write-Error "Pass -SuiteCommit and -ResultsDirectory."
        exit 1
    }

    $labviewRoot = 'C:\Program Files\National Instruments\LabVIEW 2026'
    $labview = Join-Path $labviewRoot 'LabVIEW.exe'
    $cli = 'C:\Program Files (x86)\National Instruments\Shared\LabVIEW CLI\LabVIEWCLI.exe'
    # Deliberately OUTSIDE C:\workspace (the host-bind-mounted repo checkout):
    # a Move-Item of the extracted suite folder into that SMB-backed mount
    # failed with "Access to the path ... is denied" on a real live-dispatch
    # run -- Windows-container bind mounts are known to behave differently
    # from local NTFS for rename/move operations. The suite under test does
    # not need to live inside the mount; only $ResultsDirectory does (so the
    # host can read artifacts after the container exits).
    $workspace = 'C:\lunit-suite-under-test'

    try {
        if (-not (Test-Path -LiteralPath $labview -PathType Leaf)) { throw "LabVIEW executable not found: $labview" }
        if (-not (Test-Path -LiteralPath $cli -PathType Leaf)) { throw "LabVIEW CLI executable not found: $cli" }

        New-Item -ItemType Directory -Path $ResultsDirectory -Force | Out-Null
        Assert-NoVipmPresence

        if (Test-Path -LiteralPath $workspace) { Remove-Item -LiteralPath $workspace -Recurse -Force }
    New-Item -ItemType Directory -Path $workspace -Force | Out-Null
    # Downloaded as a GitHub commit archive (not `git clone`): the NI LabVIEW
    # base image does not include git, and an archive for an exact commit SHA
    # is inherently that commit -- no separate rev-parse verification needed,
    # unlike a branch/tag clone which could move.
    Get-PinnedSuiteArchive -Owner 'ni' -Repo 'labview-icon-editor' -Commit $SuiteCommit -Destination $workspace

    $project = Join-Path $workspace 'lv_icon_editor.lvproj'
    if (-not (Test-Path -LiteralPath $project -PathType Leaf)) {
        throw "Pinned suite commit $SuiteCommit does not contain lv_icon_editor.lvproj."
    }

    $report = Join-Path $ResultsDirectory 'lunit.xml'
    if (Test-Path -LiteralPath $report) { Remove-Item -LiteralPath $report -Force }

    Start-TestLabVIEW -Project $project
    & $cli -OperationName LUnit -LabVIEWPath $labview -PortNumber 3363 `
        -AdditionalOperationDirectory (Join-Path $labviewRoot 'vi.lib\Astemes\LUnit CLI') `
        -Path $project -ReportPath $report -LogToConsole TRUE -Headless
        if ($LASTEXITCODE -ne 0) { throw "LUnit CLI failed with exit code $LASTEXITCODE." }

        $result = Test-JUnitReportAcceptable -ReportPath $report
        $result | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $ResultsDirectory 'gate-result.json') -Encoding UTF8
        Write-Host "LUnit gate PASS: $($result.totalTests) test case(s), $($result.waivedTests.Count) named waiver(s)."
    }
    finally {
        Stop-TestLabVIEW
        Get-ChildItem -LiteralPath $env:TEMP -Filter 'lvtemporary_*.log' -File -ErrorAction SilentlyContinue |
            Copy-Item -Destination $ResultsDirectory -Force -ErrorAction SilentlyContinue
    }
}
