<#
.SYNOPSIS
Verifies a downloaded pipeline artifact against its run record, project config and vipm.toml.

.DESCRIPTION
Checks the per-project output written by the native, Linux container, or Windows
container pipeline: built .vip files against their build reports and package specs,
SBOM components, hashes, purls and dependency references, source revision and patch
provenance, and the Grype and Dependency-Track reports of every stage that ran.
pipeline-run.json says which stages completed, were skipped (and why) or never ran,
so a skipped stage is not mistaken for missing output.

Exit codes: 0 artifact intact and pipeline succeeded; 1 integrity or build failure;
2 artifact intact, vulnerability-policy failure; 3 artifact intact, reporting
failure (Dependency-Track upload or findings export).

.EXAMPLE
./Test-SbomArtifact.ps1 -ArtifactPath ./artifact.zip -PipelineConfigPath ./config/vit-reuse.json -VipmTomlPath ./config/vit-reuse.toml
#>
[CmdletBinding()]
param(
    [string]$ArtifactPath,
    [string]$PipelineConfigPath,
    [string]$VipmTomlPath,
    [string]$ProjectName,
    [string]$RepositoryRoot = $PSScriptRoot,
    [string]$ReportingMode,
    [switch]$RequireTranscript,
    [string]$SummaryPath
)

. ([System.IO.Path]::Combine($PSScriptRoot, "Build-Sbom.ps1"))

$script:SbomArtifactStages = @("checkout", "packages", "sbom", "grype", "dependencyTrack")

function Get-SbomArtifactValue {
    param($Object, [string]$Name)
    if ($null -eq $Object) { return $null }
    if ($Object -is [System.Collections.IDictionary]) { return $Object[$Name] }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $null }
    return $property.Value
}

function Read-SbomArtifactJson {
    param([string]$Path, [string]$Label, [System.Collections.Generic.List[string]]$Errors)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        $Errors.Add("$Label is missing: $Path")
        return $null
    }
    try {
        $value = ConvertFrom-Json -InputObject ([System.IO.File]::ReadAllText($Path))
    } catch {
        $Errors.Add("$Label is not valid JSON (truncated?): $($_.Exception.Message)")
        return $null
    }
    if ($null -eq $value) { $Errors.Add("$Label is empty.") }
    return $value
}

function Get-SbomArtifactSha256 {
    param([string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Resolve-SbomArtifactRoot {
    # Returns the directory that holds <project>/, extracting a .zip into $TempRoot first.
    param([string]$Path, [string]$Project, [string]$TempRoot)
    if (-not (Test-Path -LiteralPath $Path)) { throw "Artifact '$Path' does not exist." }
    $root = (Resolve-Path -LiteralPath $Path).ProviderPath
    if (Test-Path -LiteralPath $root -PathType Leaf) {
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        try {
            [System.IO.Compression.ZipFile]::ExtractToDirectory($root, $TempRoot)
        } catch {
            $reason = if ($_.Exception.InnerException) { $_.Exception.InnerException.Message } else { $_.Exception.Message }
            throw "Artifact '$Path' is not a readable zip archive (truncated or corrupt): $reason"
        }
        $root = $TempRoot
    }
    if (-not (Test-Path -LiteralPath ([System.IO.Path]::Combine($root, $Project)) -PathType Container)) {
        # Tolerate one wrapping folder, as left by unpacking a downloaded zip into its own folder.
        $children = @(Get-ChildItem -LiteralPath $root -Force)
        if ($children.Count -eq 1 -and $children[0].PSIsContainer -and
            (Test-Path -LiteralPath ([System.IO.Path]::Combine($children[0].FullName, $Project)) -PathType Container)) {
            $root = $children[0].FullName
        }
    }
    return $root
}

function Get-SbomArtifactProject {
    param($Config, [string]$Name)
    $projects = @(Get-SbomArtifactValue $Config "projects" | Where-Object { $_ -and (Get-SbomArtifactValue $_ "enabled") -ne $false })
    if ($Name) {
        $match = @($projects | Where-Object { ([string](Get-SbomArtifactValue $_ "name")).Trim() -eq $Name })
        if ($match.Count -ne 1) { throw "Pipeline config has no enabled project named '$Name'." }
        return $match[0]
    }
    if ($projects.Count -ne 1) { throw "Pipeline config has $($projects.Count) enabled projects; pass -ProjectName." }
    return $projects[0]
}

$script:SbomArtifactPackageStatuses = @("built", "installation-failed", "verification-failed", "verified-installed")

function Get-SbomArtifactField {
    param($Object)
    if ($Object -is [System.Collections.IDictionary]) {
        return @($Object.Keys | ForEach-Object { [string]$_ })
    }
    return @($Object.PSObject.Properties | ForEach-Object { $_.Name })
}

function Test-SbomArtifactFailureDiagnostic {
    param($Diagnostic, [string]$Label, [System.Collections.Generic.List[string]]$Errors)

    $diagnosticFields = @(Get-SbomArtifactField $Diagnostic)
    $allowedDiagnosticFields = @("SchemaVersion", "Signature", "CapturedAtUtc", "ProcessCapture", "VipmDebugLog")
    $unexpectedFields = @($diagnosticFields | Where-Object { $_ -notin $allowedDiagnosticFields })
    if ($unexpectedFields.Count -gt 0) {
        $Errors.Add("$Label has unexpected FailureDiagnostics field(s): $($unexpectedFields -join ', ').")
    }
    if ((Get-SbomArtifactValue $Diagnostic "SchemaVersion") -ne 1 -or
        (Get-SbomArtifactValue $Diagnostic "Signature") -ne "library_add_package_no_progress_60s") {
        $Errors.Add("$Label has an unsupported FailureDiagnostics schema or signature.")
    }

    $capturedAt = [string](Get-SbomArtifactValue $Diagnostic "CapturedAtUtc")
    $parsedTimestamp = [DateTimeOffset]::MinValue
    if (-not [DateTimeOffset]::TryParse(
            $capturedAt,
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::RoundtripKind,
            [ref]$parsedTimestamp
        )) {
        $Errors.Add("$Label has an invalid FailureDiagnostics timestamp.")
    }

    $capture = Get-SbomArtifactValue $Diagnostic "ProcessCapture"
    $captureFields = @(Get-SbomArtifactField $capture)
    $unexpectedCaptureFields = @($captureFields | Where-Object { $_ -notin @("Status", "DurationMilliseconds", "ErrorType", "Processes") })
    if ($unexpectedCaptureFields.Count -gt 0) {
        $Errors.Add("$Label has unexpected process diagnostic field(s): $($unexpectedCaptureFields -join ', ').")
    }
    $captureStatus = [string](Get-SbomArtifactValue $capture "Status")
    $captureDuration = Get-SbomArtifactValue $capture "DurationMilliseconds"
    if ($captureStatus -notin @("captured", "failed", "timed-out") -or
        $captureDuration -isnot [ValueType] -or [long]$captureDuration -lt 0 -or [long]$captureDuration -gt 15000) {
        $Errors.Add("$Label has invalid bounded process-capture status or duration.")
    }
    $processes = @(Get-SbomArtifactValue $capture "Processes")
    if ($processes.Count -gt 16) {
        $Errors.Add("$Label has too many processes in FailureDiagnostics.")
    }
    foreach ($process in $processes | Select-Object -First 16) {
        $processFields = @(Get-SbomArtifactField $process)
        $unexpectedProcessFields = @($processFields | Where-Object { $_ -notin @("ProcessName", "ProcessId", "Responding", "WindowState") })
        if ($unexpectedProcessFields.Count -gt 0) {
            $Errors.Add("$Label has unexpected per-process diagnostic field(s): $($unexpectedProcessFields -join ', ').")
            continue
        }
        $processName = [string](Get-SbomArtifactValue $process "ProcessName")
        $processId = Get-SbomArtifactValue $process "ProcessId"
        $responding = Get-SbomArtifactValue $process "Responding"
        $windowState = [string](Get-SbomArtifactValue $process "WindowState")
        if ($processName -notin @("LabVIEW", "VIPM", "vipm", "vipm-cli", "VIPMDesktop", "unavailable") -or
            $processId -isnot [ValueType] -or [int]$processId -le 0 -or
            ($null -ne $responding -and $responding -isnot [bool]) -or
            $windowState -notin @("unavailable", "no-main-window", "attention-or-dialog-suspected", "main-window-present")) {
            $Errors.Add("$Label has invalid redacted process state in FailureDiagnostics.")
        }
    }

    $logState = Get-SbomArtifactValue $Diagnostic "VipmDebugLog"
    $logFields = @(Get-SbomArtifactField $logState)
    $allowedLogFields = @("Status", "Bytes", "LastWriteTimeUtc", "LinesRead", "ErrorLines", "WarningLines", "TimeoutLines", "ExceptionLines", "ErrorType")
    $unexpectedLogFields = @($logFields | Where-Object { $_ -notin $allowedLogFields })
    if ($unexpectedLogFields.Count -gt 0) {
        $Errors.Add("$Label has unexpected VIPM log diagnostic field(s): $($unexpectedLogFields -join ', ').")
    }
    $logStatus = [string](Get-SbomArtifactValue $logState "Status")
    $logBytes = Get-SbomArtifactValue $logState "Bytes"
    if ($logStatus -notin @("missing", "metadata-only", "summary-skipped-size-limit", "summary-captured", "failed") -or
        ($null -ne $logBytes -and ($logBytes -isnot [ValueType] -or [long]$logBytes -lt 0))) {
        $Errors.Add("$Label has invalid VIPM debug-log diagnostic state.")
    }
    foreach ($counterName in @("LinesRead", "ErrorLines", "WarningLines", "TimeoutLines", "ExceptionLines")) {
        $counter = Get-SbomArtifactValue $logState $counterName
        if ($counter -isnot [ValueType] -or [long]$counter -lt 0) {
            $Errors.Add("$Label has invalid VIPM debug-log summary counter '$counterName'.")
        }
    }
}

function Test-SbomArtifactPackage {
    <#
    Checks packages/ against build reports and .vip specs; returns the verified
    reports. A completed stage must cover every vipb target in vipm.toml with a
    verified-installed report. A failed stage keeps whatever was built: each
    retained .vip must still match its report, whose Status records how far it got.
    #>
    param(
        [string]$ProjectDir,
        [string]$TomlPath,
        [System.Collections.Generic.List[string]]$Errors,
        [System.Collections.Generic.List[string]]$Notes,
        [switch]$StageFailed
    )
    $packagesDir = [System.IO.Path]::Combine($ProjectDir, "packages")
    if (-not (Test-Path -LiteralPath $packagesDir -PathType Container)) {
        if ($StageFailed) { $Notes.Add("packages stage failed before any package was built.") }
        else { $Errors.Add("packages stage completed but packages/ is missing.") }
        return @()
    }
    if (-not $StageFailed -and (-not $TomlPath -or -not (Test-Path -LiteralPath $TomlPath -PathType Leaf))) {
        $Errors.Add("packages stage completed but no vipm.toml was supplied to determine the expected packages.")
        return @()
    }
    $expectedTargets = @()
    if (-not $StageFailed) {
        $expectedTargets = @((Read-VipmBuildTarget -TomlPath $TomlPath).Values |
                Where-Object { ([string]$_["type"]).ToLowerInvariant() -eq "vipb" } | ForEach-Object { [string]$_["name"] })
    }
    $reports = [System.Collections.Generic.List[object]]::new()
    $expectedFiles = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($file in Get-ChildItem -LiteralPath $packagesDir -Filter "*.json" -File) {
        [void]$expectedFiles.Add($file.Name)
        $report = Read-SbomArtifactJson -Path $file.FullName -Label "Build report $($file.Name)" -Errors $Errors
        if ($null -eq $report) { continue }
        $missingFields = @("Target", "Name", "Version", "VipFile", "Sha256" | Where-Object { -not (Get-SbomArtifactValue $report $_) })
        if ($missingFields.Count -gt 0) {
            $Errors.Add("Build report $($file.Name) is missing $($missingFields -join ', ').")
            continue
        }
        $label = "$($report.Name) $($report.Version)"
        $packageStatus = [string](Get-SbomArtifactValue $report "Status")
        $installError = [string](Get-SbomArtifactValue (Get-SbomArtifactValue $report "Installation") "Error")
        if (-not $packageStatus) {
            # Reports written before Status existed were only written after a verified installation.
            $packageStatus = "verified-installed"
            $Notes.Add("$label build report has no Status (legacy format); it is treated as verified-installed.")
        } elseif ($script:SbomArtifactPackageStatuses -notcontains $packageStatus) {
            $Errors.Add("$label build report has unknown Status '$packageStatus'.")
        } elseif ($packageStatus -like "*-failed" -and -not $installError) {
            $Errors.Add("$label build report Status is $packageStatus but records no Installation.Error.")
        }
        if ($StageFailed) {
            $Notes.Add("Retained package $label`: $packageStatus$(if ($installError) { " ($installError)" })")
        } elseif ($packageStatus -ne "verified-installed") {
            $Errors.Add("$label build report Status is '$packageStatus' although the packages stage completed.")
        }
        if ($file.Name -ne "$($report.Name).json") { $Errors.Add("Build report $($file.Name) describes $($report.Name).") }
        if ($report.VipFile -ne "$($report.Name)-$($report.Version).vip") {
            $Errors.Add("$label build report names '$($report.VipFile)', expected '$($report.Name)-$($report.Version).vip'.")
        }
        [void]$expectedFiles.Add([string]$report.VipFile)
        $debugTrace = [string](Get-SbomArtifactValue (Get-SbomArtifactValue $report "Installation") "DebugTrace")
        if ($debugTrace) {
            $tracePrefix = [System.IO.Path]::GetFileNameWithoutExtension([string]$report.VipFile) + "-vipm-debug"
            $tracePattern = "^$([regex]::Escape($tracePrefix))(?:-\d{8}-\d{6}-\d{3})?\.log$"
            if ($debugTrace -notmatch $tracePattern) {
                $Errors.Add("$label build report has invalid Installation.DebugTrace '$debugTrace'.")
            } else {
                [void]$expectedFiles.Add($debugTrace)
                $debugTracePath = [System.IO.Path]::Combine($packagesDir, $debugTrace)
                if (-not (Test-Path -LiteralPath $debugTracePath -PathType Leaf)) {
                    $Errors.Add("$label VIPM debug trace '$debugTrace' is missing.")
                }
            }
        }
        $failureDiagnostics = Get-SbomArtifactValue (Get-SbomArtifactValue $report "Installation") "FailureDiagnostics"
        if ($null -ne $failureDiagnostics) {
            Test-SbomArtifactFailureDiagnostic -Diagnostic $failureDiagnostics -Label $label -Errors $Errors
        }
        $vipPath = [System.IO.Path]::Combine($packagesDir, [string]$report.VipFile)
        $report | Add-Member -NotePropertyName ActualSha256 -NotePropertyValue $null -Force
        if (-not (Test-Path -LiteralPath $vipPath -PathType Leaf)) {
            $Errors.Add("$label package $($report.VipFile) is missing.")
        } else {
            $report.ActualSha256 = Get-SbomArtifactSha256 -Path $vipPath
            if ($report.ActualSha256 -ne ([string]$report.Sha256).ToLowerInvariant()) {
                $Errors.Add("$label SHA-256 mismatch: $($report.VipFile) is $($report.ActualSha256), build report says $($report.Sha256).")
            }
            try {
                $spec = Read-VipPackageSpec -VipPath $vipPath
                if ($spec.Name -ne $report.Name -or $spec.Version -ne $report.Version) {
                    $Errors.Add("$label version mismatch: $($report.VipFile) spec is $($spec.Name) $($spec.Version).")
                }
                $reportRequires = @($report.Requires | Where-Object { $_ } | Sort-Object) -join ","
                if ((@($spec.Requires | Sort-Object) -join ",") -ne $reportRequires) {
                    $Errors.Add("$label Requires mismatch: spec has '$($spec.Requires -join ', ')', build report has '$($report.Requires -join ', ')'.")
                }
            } catch {
                $Errors.Add("$label package $($report.VipFile) cannot be read (truncated or corrupt): $($_.Exception.Message)")
            }
        }
        $reports.Add($report)
    }
    foreach ($duplicate in @($reports | Group-Object Target | Where-Object Count -gt 1)) {
        $Errors.Add("Build target '$($duplicate.Name)' has $($duplicate.Count) build reports.")
    }
    if (-not $StageFailed) {
        $actualTargets = @($reports | ForEach-Object { [string]$_.Target } | Sort-Object -Unique)
        foreach ($target in $expectedTargets | Where-Object { $actualTargets -notcontains $_ }) {
            $Errors.Add("Expected package for vipm.toml target '$target' has no build report.")
        }
        foreach ($target in $actualTargets | Where-Object { $expectedTargets -notcontains $_ }) {
            $Errors.Add("Build report for target '$target' is not a vipb target in vipm.toml.")
        }
    }
    foreach ($file in Get-ChildItem -LiteralPath $packagesDir -File | Where-Object { -not $expectedFiles.Contains($_.Name) }) {
        $Errors.Add("Unexpected file in packages/: $($file.Name).")
    }
    return $reports.ToArray()
}

function Test-SbomArtifactBom {
    param([string]$SbomPath, $Record, [object[]]$Reports, [System.Collections.Generic.List[string]]$Errors)
    $sbom = Read-SbomArtifactJson -Path $SbomPath -Label "SBOM" -Errors $Errors
    if ($null -eq $sbom) { return }
    if ($sbom.bomFormat -ne "CycloneDX") { $Errors.Add("SBOM bomFormat is '$($sbom.bomFormat)', expected CycloneDX.") }
    $rootRef = [string](Get-SbomArtifactValue $sbom.metadata.component "bom-ref")
    if (-not $rootRef) { $Errors.Add("SBOM metadata.component has no bom-ref.") }

    $refs = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::Ordinal)
    if ($rootRef) { [void]$refs.Add($rootRef) }
    $byPurl = @{}
    $components = @($sbom.components | Where-Object { $_ })
    foreach ($component in $components) {
        $ref = [string](Get-SbomArtifactValue $component "bom-ref")
        if (-not $ref) {
            $Errors.Add("SBOM component '$($component.name)' has no bom-ref.")
        } elseif (-not $refs.Add($ref)) {
            $Errors.Add("SBOM bom-ref '$ref' is not unique.")
        }
        if ($component.purl) {
            $purl = [System.Uri]::UnescapeDataString([string]$component.purl)
            if (-not $byPurl.ContainsKey($purl)) { $byPurl[$purl] = [System.Collections.Generic.List[object]]::new() }
            $byPurl[$purl].Add($component)
        }
    }
    foreach ($purl in $byPurl.Keys | Where-Object { $byPurl[$_].Count -gt 1 }) {
        $Errors.Add("SBOM purl '$purl' appears $($byPurl[$purl].Count) times.")
    }
    foreach ($node in @($sbom.dependencies | Where-Object { $_ })) {
        if (-not $refs.Contains([string]$node.ref)) { $Errors.Add("SBOM dependency ref '$($node.ref)' is not a component.") }
        foreach ($target in @($node.dependsOn | Where-Object { $_ })) {
            if (-not $refs.Contains([string]$target)) { $Errors.Add("SBOM dependency '$($node.ref)' -> '$target' is not a component.") }
        }
    }

    $revision = [string](Get-SbomArtifactValue $Record.source "revision")
    $metadata = @{}
    foreach ($property in @($sbom.metadata.properties | Where-Object { $_ })) {
        if (-not $metadata.ContainsKey([string]$property.name)) { $metadata[[string]$property.name] = [System.Collections.Generic.List[string]]::new() }
        $metadata[[string]$property.name].Add([string]$property.value)
    }
    $manifestOnly = $metadata.ContainsKey("sbom:source") -and $metadata["sbom:source"].Count -eq 1 -and `
        $metadata["sbom:source"][0] -ceq "vipm.toml (manifest mode, no vipm sbom scan)"
    if ($Record.pipeline -eq "linux-container" -and -not $manifestOnly -and
        -not (Test-Path -LiteralPath ([System.IO.Path]::Combine([System.IO.Path]::GetDirectoryName($SbomPath), "vipm.lock")) -PathType Leaf)) {
        $Errors.Add("Linux VIPM SBOM requires vipm.lock, but it is missing.")
    }
    if ($metadata.ContainsKey("vcs:revision") -and $metadata["vcs:revision"][0] -ne $revision) {
        $Errors.Add("SBOM vcs:revision '$($metadata["vcs:revision"][0])' does not match the checked-out revision '$revision'.")
    }
    $expectedPatches = @($Record.patches | Where-Object { $_ } | ForEach-Object { "$($_.path) sha256:$($_.sha256)" } | Sort-Object)
    $sbomPatches = @($(if ($metadata.ContainsKey("checkout:patch")) { $metadata["checkout:patch"] }) | Sort-Object)
    if (($expectedPatches -join "`n") -ne ($sbomPatches -join "`n")) {
        $Errors.Add("SBOM checkout:patch provenance ($($sbomPatches -join '; ')) does not match the applied patches ($($expectedPatches -join '; ')).")
    }

    foreach ($report in $Reports) {
        $purl = "pkg:vipm/$($report.Name)@$($report.Version)"
        $matched = @($(if ($byPurl.ContainsKey($purl)) { $byPurl[$purl] }))
        if ($matched.Count -ne 1) {
            $Errors.Add("SBOM must contain $purl exactly once (found $($matched.Count)).")
            continue
        }
        $component = $matched[0]
        $hash = @($component.hashes | Where-Object { $_.alg -eq "SHA-256" } | ForEach-Object { ([string]$_.content).ToLowerInvariant() })
        $expected = if ($report.ActualSha256) { $report.ActualSha256 } else { ([string]$report.Sha256).ToLowerInvariant() }
        if ($hash.Count -ne 1 -or $hash[0] -ne $expected) {
            $Errors.Add("SBOM SHA-256 for $purl is '$($hash -join ', ')', expected $expected.")
        }
        $properties = @{}
        foreach ($property in @($component.properties | Where-Object { $_ })) { $properties[[string]$property.name] = [string]$property.value }
        if ($properties["build:revision"] -ne $revision) {
            $Errors.Add("SBOM build:revision for $purl is '$($properties["build:revision"])', expected '$revision'.")
        }
        if ($properties["build:vip"] -ne $report.VipFile) {
            $Errors.Add("SBOM build:vip for $purl is '$($properties["build:vip"])', expected '$($report.VipFile)'.")
        }
        if ($properties["build:target"] -ne $report.Target) {
            $Errors.Add("SBOM build:target for $purl is '$($properties["build:target"])', expected '$($report.Target)'.")
        }
    }
}

function Test-SbomArtifactOutput {
    param([string]$ProjectDir, $Record, [System.Collections.Generic.List[string]]$Errors)
    $outputs = $null
    if ($Record -is [System.Collections.IDictionary]) {
        if ($Record.Contains("outputs")) { $outputs = $Record["outputs"] }
    } else {
        $outputProperty = $Record.PSObject.Properties["outputs"]
        if ($null -ne $outputProperty) { $outputs = $outputProperty.Value }
    }
    if ($null -eq $outputs -or $outputs -isnot [array]) {
        $Errors.Add("Linux run record has no output hash list.")
        return $false
    }
    $projectItem = Get-Item -LiteralPath $ProjectDir -Force -ErrorAction Stop
    if ($projectItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
        $Errors.Add("Linux project output directory is a link.")
        return $false
    }
    $linkedItems = @(Get-ChildItem -LiteralPath $ProjectDir -Recurse -Force -ErrorAction Stop |
        Where-Object { $_.Attributes -band [System.IO.FileAttributes]::ReparsePoint })
    if ($linkedItems.Count -gt 0) {
        foreach ($linkedItem in $linkedItems) {
            $relative = [System.IO.Path]::GetRelativePath($ProjectDir, $linkedItem.FullName).Replace('\', '/')
            $Errors.Add("Linux output '$relative' is a link or traverses a link.")
        }
        return $false
    }
    $expected = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    $root = [System.IO.Path]::GetFullPath($ProjectDir).TrimEnd([System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
    foreach ($output in @($outputs | Where-Object { $_ })) {
        $relative = [string](Get-SbomArtifactValue $output "path")
        $sha256 = [string](Get-SbomArtifactValue $output "sha256")
        $segments = $relative.Split('/')
        if (-not $relative -or $relative.Contains('\') -or $relative.StartsWith('/') -or
            $relative -match '^[A-Za-z]:' -or $segments -contains '.' -or $segments -contains '..' -or
            $segments -contains '' -or $relative -eq 'pipeline-run.json') {
            $Errors.Add("Linux run record has an unsafe output path '$relative'.")
            continue
        }
        if ($sha256 -notmatch '^[0-9a-fA-F]{64}$') {
            $Errors.Add("Linux run record has an invalid SHA-256 for '$relative'.")
            continue
        }
        $path = [System.IO.Path]::GetFullPath([System.IO.Path]::Combine($ProjectDir,
            $relative.Replace('/', [System.IO.Path]::DirectorySeparatorChar)))
        if (-not $path.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) {
            $Errors.Add("Linux run record output '$relative' escapes the project folder.")
            continue
        }
        if (-not $expected.Add($relative)) {
            $Errors.Add("Linux run record repeats output '$relative'.")
            continue
        }
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            $Errors.Add("Linux output '$relative' is missing from the downloaded artifact.")
        } elseif ((Get-SbomArtifactSha256 -Path $path) -ne $sha256.ToLowerInvariant()) {
            $Errors.Add("Linux output '$relative' SHA-256 does not match the run record.")
        }
    }
    foreach ($file in Get-ChildItem -LiteralPath $ProjectDir -Recurse -File | Where-Object Name -ne 'pipeline-run.json') {
        $relative = [System.IO.Path]::GetRelativePath($ProjectDir, $file.FullName).Replace('\', '/')
        if (-not $expected.Contains($relative)) {
            $Errors.Add("Linux output '$relative' is not covered by the run record hash list.")
        }
    }
    return $true
}

function Test-SbomArtifactIntegrity {
    <#
    Verifies one project's artifact output and returns the outcome: ExitCode,
    Outcome (succeeded | integrity | build | policy | reporting), Errors and Notes.
    #>
    param(
        [string]$ArtifactPath,
        [string]$PipelineConfigPath,
        [string]$VipmTomlPath,
        [string]$ProjectName,
        [string]$RepositoryRoot,
        [string]$ReportingMode,
        [switch]$RequireTranscript
    )
    $errors = [System.Collections.Generic.List[string]]::new()
    $notes = [System.Collections.Generic.List[string]]::new()
    $result = [PSCustomObject]@{
        Project = $ProjectName; Pipeline = $null; Outcome = "integrity"; ExitCode = 1
        Stages = [ordered]@{}; Errors = $errors; Notes = $notes
    }
    $tempRoot = [System.IO.Path]::Combine([System.IO.Path]::GetTempPath(), "sbom-artifact-" + [guid]::NewGuid().ToString("N"))
    try {
        $config = Read-SbomArtifactJson -Path $PipelineConfigPath -Label "Pipeline config" -Errors $errors
        if ($null -eq $config) { return $result }
        $project = Get-SbomArtifactProject -Config $config -Name $ProjectName
        $name = ([string]$project.name).Trim()
        $result.Project = $name
        $root = Resolve-SbomArtifactRoot -Path $ArtifactPath -Project $name -TempRoot $tempRoot
        $projectDir = [System.IO.Path]::Combine($root, $name)

        if ($RequireTranscript) {
            $transcript = [System.IO.Path]::Combine($root, "workflow-build.log")
            if (-not (Test-Path -LiteralPath $transcript -PathType Leaf)) {
                $errors.Add("Workflow transcript workflow-build.log is missing.")
            } elseif ((Get-Item -LiteralPath $transcript -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
                $errors.Add("Workflow transcript workflow-build.log is a link.")
                return $result
            } elseif ([System.IO.File]::ReadAllText($transcript) -notmatch "PowerShell transcript end") {
                $errors.Add("Workflow transcript workflow-build.log is truncated (no 'PowerShell transcript end').")
            }
        }
        if (-not (Test-Path -LiteralPath $projectDir -PathType Container)) {
            $errors.Add("Artifact has no output folder for project '$name'.")
            return $result
        }
        if ((Get-Item -LiteralPath $projectDir -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
            $errors.Add("Artifact project output directory is a link.")
            return $result
        }
        $recordPath = [System.IO.Path]::Combine($projectDir, "pipeline-run.json")
        $recordItem = Get-Item -LiteralPath $recordPath -Force -ErrorAction SilentlyContinue
        if ($null -ne $recordItem -and ($recordItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
            $errors.Add("Run record pipeline-run.json is a link.")
            return $result
        }
        $record = Read-SbomArtifactJson -Path $recordPath -Label "Run record pipeline-run.json" -Errors $errors
        if ($null -eq $record) { return $result }
        $result.Pipeline = [string]$record.pipeline
            $result.Pipeline = [string]$record.pipeline
            $recordMode = ([string](Get-SbomArtifactValue $record "reportingMode")).Trim().ToLowerInvariant()
            $expectedMode = if ([string]::IsNullOrWhiteSpace($ReportingMode)) { $recordMode } else { $ReportingMode.Trim().ToLowerInvariant() }
            if ($expectedMode -notin @("full", "hosted-grype")) { $errors.Add("Reporting mode '$expectedMode' is unknown.") }
            if ($recordMode -notin @("full", "hosted-grype")) { $errors.Add("Run record has no valid reportingMode.") }
            elseif ($expectedMode -and $recordMode -ne $expectedMode) { $errors.Add("Run record reportingMode '$recordMode' does not match expected '$expectedMode'.") }
        if ($record.schemaVersion -ne 1) { $errors.Add("Run record schemaVersion '$($record.schemaVersion)' is not supported.") }
        if ($record.project -ne $name) { $errors.Add("Run record is for project '$($record.project)', expected '$name'.") }
        if (@("native", "windows-container", "linux-container") -notcontains $record.pipeline) { $errors.Add("Run record pipeline '$($record.pipeline)' is unknown.") }
        if (@("succeeded", "failed") -notcontains $record.status) {
            $errors.Add("Run record status is '$($record.status)'; the pipeline did not finish.")
            return $result
        }
        if ($record.pipeline -eq "linux-container") {
            $safeOutputs = Test-SbomArtifactOutput -ProjectDir $projectDir -Record $record -Errors $errors
            if (-not $safeOutputs) { return $result }
        }

        $status = @{}
        foreach ($stage in $script:SbomArtifactStages) {
            $entry = Get-SbomArtifactValue $record.stages $stage
            $status[$stage] = if ($null -eq $entry) { "not-reached" } else { [string]$entry.status }
            $result.Stages[$stage] = [ordered]@{ Status = $status[$stage]; Gate = [string](Get-SbomArtifactValue $entry "gate"); Reason = [string](Get-SbomArtifactValue $entry "reason") }
            if ($status[$stage] -eq "skipped") {
                if (-not $result.Stages[$stage].Reason) { $errors.Add("Stage '$stage' is skipped without a reason.") }
                else { $notes.Add("Stage '$stage' skipped: $($result.Stages[$stage].Reason)") }
            } elseif ($status[$stage] -eq "not-reached") {
                if ($record.status -eq "succeeded") { $errors.Add("Stage '$stage' is not recorded although the pipeline succeeded.") }
                else { $notes.Add("Stage '$stage' was not reached; its output is not expected.") }
            } elseif ($status[$stage] -eq "failed") {
                $notes.Add("Stage '$stage' failed; its output is not verified.")
            } elseif ($status[$stage] -ne "completed") {
                $errors.Add("Stage '$stage' has unknown status '$($status[$stage])'.")
            }
        }
        $dependencyTrackStatus = $status["dependencyTrack"]
        $dependencyTrackGate = $result.Stages["dependencyTrack"].Gate
        if ($expectedMode -eq "full") {
            if ($dependencyTrackStatus -eq "skipped") { $errors.Add("Full reporting cannot skip the Dependency-Track stage.") }
            if ($dependencyTrackStatus -eq "completed" -and $dependencyTrackGate -notin @("passed", "failed")) {
                $errors.Add("Full reporting requires a fresh evaluated Dependency-Track gate; found '$dependencyTrackGate'.")
            }
        } elseif ($expectedMode -eq "hosted-grype") {
            if ($record.status -eq "succeeded" -and $dependencyTrackStatus -ne "skipped") {
                $errors.Add("Hosted Grype-only success must record Dependency-Track as skipped.")
            }
            if ($dependencyTrackStatus -eq "completed") {
                $errors.Add("Hosted Grype-only mode must not publish to Dependency-Track.")
            }
        }
        $buildPackages = (Get-SbomArtifactValue (Get-SbomArtifactValue $project "build") "packages") -eq $true
        if ($record.pipeline -ne "linux-container" -and $buildPackages -and $status["packages"] -eq "skipped") {
            $errors.Add("build.packages is enabled but the packages stage was skipped.")
        }

        if ($status["checkout"] -eq "completed") {
            if (-not (Get-SbomArtifactValue $record.source "revision")) { $errors.Add("Run record has no source revision.") }
            if (-not (Get-SbomArtifactValue $record.source "url")) { $errors.Add("Run record has no source URL.") }
            $configured = @(Get-SbomArtifactValue $project "patches" | Where-Object { $_ } | ForEach-Object { ([string]$_).Trim() } | Sort-Object)
            $recorded = @($record.patches | Where-Object { $_ } | ForEach-Object { [string]$_.path } | Sort-Object)
            if (($configured -join "`n") -ne ($recorded -join "`n")) {
                $errors.Add("Applied patches ($($recorded -join ', ')) do not match the configured patches ($($configured -join ', ')).")
            }
            foreach ($patch in @($record.patches | Where-Object { $_ })) {
                $patchPath = [System.IO.Path]::Combine($RepositoryRoot, [string]$patch.path)
                if (-not (Test-Path -LiteralPath $patchPath -PathType Leaf)) {
                    $errors.Add("Patch '$($patch.path)' is not in the repository at '$RepositoryRoot'.")
                } elseif ((Get-SbomArtifactSha256 -Path $patchPath) -ne ([string]$patch.sha256).ToLowerInvariant()) {
                    $errors.Add("Patch '$($patch.path)' SHA-256 $($patch.sha256) does not match the repository copy.")
                }
            }
        }

        $reports = @()
        if ($status["packages"] -eq "completed") {
            $reports = @(Test-SbomArtifactPackage -ProjectDir $projectDir -TomlPath $VipmTomlPath -Errors $errors -Notes $notes)
            $notes.Add("Verified packages: $(($reports | ForEach-Object { "$($_.Name) $($_.Version)" }) -join ', ').")
        } elseif ($status["packages"] -eq "failed") {
            $null = Test-SbomArtifactPackage -ProjectDir $projectDir -TomlPath $VipmTomlPath -Errors $errors -Notes $notes -StageFailed
        }

        $sboms = @(Get-ChildItem -LiteralPath $projectDir -Filter "*-sbom.json" -File)
        $prefix = $null
        if ($status["sbom"] -eq "completed") {
            if ($sboms.Count -ne 1) {
                $errors.Add("Expected exactly one *-sbom.json in '$name' (found $($sboms.Count)).")
            } else {
                $prefix = [System.IO.Path]::Combine($projectDir, $sboms[0].Name.Substring(0, $sboms[0].Name.Length - "-sbom.json".Length))
                Test-SbomArtifactBom -SbomPath $sboms[0].FullName -Record $record -Reports $reports -Errors $errors
            }
        }

        if ($status["grype"] -eq "completed" -and $prefix) {
            $text = "$prefix-vuln-report.txt"
            if (-not (Test-Path -LiteralPath $text -PathType Leaf) -or -not ([System.IO.File]::ReadAllText($text)).Trim()) {
                $errors.Add("Grype report $([System.IO.Path]::GetFileName($text)) is missing or empty.")
            }
            if ($record.pipeline -in @("windows-container", "linux-container")) {
                $grype = Read-SbomArtifactJson -Path "$prefix-vuln-report.json" -Label "Grype JSON report" -Errors $errors
                if ($null -ne $grype) {
                    if ($null -eq $grype.PSObject.Properties["matches"]) { $errors.Add("Grype JSON report has no matches array.") }
                    else { $notes.Add("Grype matches: $(@($grype.matches | Where-Object { $_ }).Count).") }
                }
            }
        }

        $exportError = [string](Get-SbomArtifactValue (Get-SbomArtifactValue $record.stages "dependencyTrack") "findingsExportError")
        if ($status["dependencyTrack"] -eq "completed" -and $prefix) {
            if ($exportError) {
                $notes.Add("Dependency-Track findings export failed during the run: $exportError")
            } else {
                $findings = Read-SbomArtifactJson -Path "$prefix-dt-findings.json" -Label "Dependency-Track findings report" -Errors $errors
                if ($null -ne $findings) {
                    if ($null -eq $findings.PSObject.Properties["findings"]) { $errors.Add("Dependency-Track findings report has no findings array.") }
                    else {
                        $severities = @($findings.findings | Where-Object { $_ } | ForEach-Object { [string]$_.vulnerability.severity })
                        $notes.Add("Dependency-Track findings: $($severities.Count) (critical $(@($severities -eq 'CRITICAL').Count), high $(@($severities -eq 'HIGH').Count)).")
                    }
                }
            }
        }

        $failedGates = @($result.Stages.Keys | Where-Object { $result.Stages[$_].Gate -eq "failed" })
        if ($record.status -eq "succeeded" -and $failedGates.Count -gt 0) {
            $errors.Add("Run record succeeded but the $($failedGates -join ', ') gate failed.")
        }
        if ($record.status -eq "failed" -and $record.failureCategory -eq "policy" -and $failedGates.Count -eq 0) {
            $errors.Add("Run record reports a policy failure but no gate failed.")
        }
        if ($errors.Count -gt 0) { return $result }

        if ($record.status -eq "failed") {
            $notes.Add("Pipeline failure ($($record.failureCategory)): $($record.failure)")
            switch ([string]$record.failureCategory) {
                "policy" { $result.Outcome = "policy"; $result.ExitCode = 2 }
                "reporting" { $result.Outcome = "reporting"; $result.ExitCode = 3 }
                default { $result.Outcome = "build"; $result.ExitCode = 1 }
            }
        } elseif ($exportError) {
            $result.Outcome = "reporting"; $result.ExitCode = 3
        } else {
            $result.Outcome = "succeeded"; $result.ExitCode = 0
        }
        return $result
    } catch {
        $errors.Add($_.Exception.Message)
        return $result
    } finally {
        if (Test-Path -LiteralPath $tempRoot) { Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue }
    }
}

function Format-SbomArtifactSummary {
    param($Result)
    $lines = [System.Collections.Generic.List[string]]::new()
    $lines.Add("## Artifact verification: $($Result.Project) ($($Result.Pipeline))")
    $lines.Add("")
    $lines.Add("Outcome: **$($Result.Outcome)** (exit $($Result.ExitCode))")
    if ($Result.Stages.Count -gt 0) {
        $lines.Add("")
        $lines.Add("| Stage | Status | Gate | Reason |")
        $lines.Add("|---|---|---|---|")
        foreach ($stage in $Result.Stages.Keys) {
            $entry = $Result.Stages[$stage]
            $lines.Add("| $stage | $($entry.Status) | $($entry.Gate) | $($entry.Reason) |")
        }
    }
    if ($Result.Errors.Count -gt 0) {
        $lines.Add("")
        $lines.Add("Integrity errors:")
        foreach ($message in $Result.Errors) { $lines.Add("- $message") }
    }
    if ($Result.Notes.Count -gt 0) {
        $lines.Add("")
        foreach ($message in $Result.Notes) { $lines.Add("- $message") }
    }
    return $lines.ToArray()
}

if ($MyInvocation.InvocationName -ne ".") {
    if (-not $ArtifactPath -or -not $PipelineConfigPath) {
        Write-Error "Pass -ArtifactPath and -PipelineConfigPath."
        exit 1
    }
    $result = Test-SbomArtifactIntegrity -ArtifactPath $ArtifactPath -PipelineConfigPath $PipelineConfigPath `
        -VipmTomlPath $VipmTomlPath -ProjectName $ProjectName -RepositoryRoot $RepositoryRoot `
        -ReportingMode $ReportingMode -RequireTranscript:$RequireTranscript
    $summary = Format-SbomArtifactSummary -Result $result
    $summary | ForEach-Object { Write-Host $_ }
    if ($SummaryPath) { Add-Content -LiteralPath $SummaryPath -Value ($summary + "") -Encoding utf8 }
    exit $result.ExitCode
}
