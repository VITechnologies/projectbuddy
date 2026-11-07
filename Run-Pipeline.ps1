<#
.SYNOPSIS
    Multi-project batch automation pipeline with debugging capabilities.
    Integrates Environment Audit, TOML Sync, VIPM SBOM extraction, Recursive NIPM Deep-Scan, 
    Full CycloneDX Dependency Graphing, Grype scanning, and Dependency-Track push.
#>

param(
    [string]$ConfigName = "",
    [switch]$DebugMode,
    # Root used to resolve project-specific paths (patches, LVLIBP release
    # dependency stage paths, the working/output directories) -- distinct
    # from $PSScriptRoot, which stays the location of this generic engine
    # script itself (Build-Sbom.ps1/internal/pipeline-config.schema.json).
    # Defaults to $PSScriptRoot so existing invocations are unaffected;
    # a consumer invoking this script from a pinned external checkout
    # (see docs/projectbuddy-consumption.md) must pass the real repo root
    # here, since project-specific files like config/patches/*.patch only
    # exist there, never in the generic engine checkout.
    [string]$WorkspaceRoot = $PSScriptRoot
)

$ErrorActionPreference = "Stop"
. ([System.IO.Path]::Combine($PSScriptRoot, "Build-Sbom.ps1"))
. ([System.IO.Path]::Combine($PSScriptRoot, "internal", "Pipeline.Stages.ps1"))

if (-not $IsWindows) {
    # Run-Pipeline.sh is NOT a Linux equivalent of this SBOM pipeline -- it is
    # a separate, unrelated LabVIEWCLI MassCompile/VI Analyzer tool for NI's
    # own example VIs. There is currently no native Linux SBOM pipeline.
    throw "Run-Pipeline.ps1 (the native SBOM pipeline) supports Windows only. There is no native Linux SBOM pipeline in this repo."
}
if ($env:VIPM_COMMUNITY_EDITION -eq "1") {
    throw "This pipeline requires VIPM Pro. Unset VIPM_COMMUNITY_EDITION and activate VIPM Pro."
}

# ==========================================
# ENTERPRISE LOGGING & DEBUG FRAMEWORK
# ==========================================
function Write-PipelineLog {
    param (
        [Parameter(Mandatory=$true)]
        [string]$Message,
        [ValidateSet("INFO", "DEBUG", "WARN", "ERROR", "SUCCESS", "METRIC", "TRACE")]
        [string]$Level = "INFO"
    )
    $timestamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss.fff")
    $prefix = "[$timestamp] [$Level]"
    
    switch ($Level) {
        "INFO"    { Write-Host "$prefix $Message" -ForegroundColor Cyan }
        "DEBUG"   { if ($DebugMode) { Write-Host "$prefix $Message" -ForegroundColor DarkGray } }
        "TRACE"   { if ($DebugMode) { Write-Host "$prefix $Message" -ForegroundColor Magenta } }
        "WARN"    { Write-Host "$prefix $Message" -ForegroundColor DarkYellow }
        "ERROR"   { Write-Host "$prefix $Message" -ForegroundColor Red }
        "SUCCESS" { Write-Host "$prefix $Message" -ForegroundColor Green }
        "METRIC"  { Write-Host "$prefix $Message" -ForegroundColor Blue }
    }
}

function Export-DebugContext {
    param(
        [string]$ProjectName, 
        [string]$OutputDir, 
        [System.Management.Automation.ErrorRecord]$ErrorRecord
    )
    if (-not [System.IO.Directory]::Exists($OutputDir)) { return }
    
    $dumpPath = [System.IO.Path]::Combine($OutputDir, "$ProjectName-crash-dump.json")
    $dump = @{
        Timestamp   = (Get-Date).ToString("o")
        Project     = $ProjectName
        Exception   = @{
            Message        = $ErrorRecord.Exception.Message
            StackTrace     = $ErrorRecord.ScriptStackTrace
            InnerException = if ($ErrorRecord.Exception.InnerException) { $ErrorRecord.Exception.InnerException.Message } else { $null }
        }
        Environment = @{
            PSVersion        = $PSVersionTable.PSVersion.ToString()
            OS               = [Environment]::OSVersion.VersionString
            WorkingDirectory = (Get-Location).Path
        }
    }
    
    $dump | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $dumpPath -Encoding utf8
    Write-PipelineLog -Message "Crash context dumped to $dumpPath" -Level DEBUG
}

Write-PipelineLog -Message "==================================================" -Level INFO
Write-PipelineLog -Message "Starting Enterprise Multi-Project Batch Pipeline" -Level INFO
if ($DebugMode) { Write-PipelineLog -Message "DEBUG MODE ENABLED: Emitting extended telemetry and crash dumps." -Level DEBUG }
Write-PipelineLog -Message "==================================================" -Level INFO

# Load configuration
$configSource = "configuration input"
try {
    if ($env:PROJECTS_OVERRIDE) {
        $configSource = "PROJECTS_OVERRIDE"
        Write-PipelineLog -Message "Using PROJECTS_OVERRIDE; local '$ConfigName' will not be read." -Level INFO
        $configText = $env:PROJECTS_OVERRIDE
    } elseif (-not [string]::IsNullOrWhiteSpace($ConfigName)) {
        $configSource = [System.IO.Path]::Combine($WorkspaceRoot, $ConfigName)
        Write-PipelineLog -Message "Using local configuration '$configSource'..." -Level INFO
        $configText = [System.IO.File]::ReadAllText($configSource)
    } else {
        throw "Set the process-level PROJECTS_OVERRIDE variable or pass -ConfigName with a local configuration file."
    }

    $schemaPath = [System.IO.Path]::Combine($PSScriptRoot, "pipeline-config.schema.json")
    $schemaText = [System.IO.File]::ReadAllText($schemaPath)
    if (-not (Test-Json -Json $configText -Schema $schemaText -ErrorAction Stop)) {
        throw "Configuration does not match pipeline-config.schema.json."
    }
    $config = ConvertFrom-Json -InputObject $configText
} catch {
    Write-PipelineLog -Message "Invalid pipeline configuration from '$configSource': $($_.Exception.Message) Expected an object containing a non-empty 'projects' array; each project requires 'name' and, unless disabled, 'vcs' ('git' or 'svn'). See docs/configuration-schema.md." -Level ERROR
    exit 1
}

$manifestDocsPath = "docs/configuration-schema.md#vipm-manifest-github-actions-variable"
$manifestDocs = if ($env:GITHUB_SERVER_URL -and $env:GITHUB_REPOSITORY) {
    "$env:GITHUB_SERVER_URL/$env:GITHUB_REPOSITORY/blob/main/$manifestDocsPath"
} else {
    $manifestDocsPath
}
$vipmManifestJson = [Environment]::GetEnvironmentVariable("VIPM_TOML_CONFIGS")
try {
    if ([string]::IsNullOrWhiteSpace($vipmManifestJson)) {
        throw "VIPM_TOML_CONFIGS is missing or blank."
    }
    $vipmManifestsByProject = ConvertFrom-Json -InputObject $vipmManifestJson -AsHashtable -NoEnumerate -ErrorAction Stop
    if ($vipmManifestsByProject -isnot [System.Collections.IDictionary]) {
        throw "VIPM_TOML_CONFIGS must be a JSON object keyed by configured project name."
    }
    foreach ($entry in $vipmManifestsByProject.GetEnumerator()) {
        if ($entry.Value -isnot [string] -or [string]::IsNullOrWhiteSpace($entry.Value)) {
            throw "VIPM_TOML_CONFIGS entry for project '$($entry.Key)' must be a non-blank TOML string."
        }
    }
    foreach ($project in $config.projects) {
        if ($project.enabled -eq $false) { continue }
        $projectName = ([string]$project.name).Trim()
        if (-not $vipmManifestsByProject.Contains($projectName)) {
            throw "VIPM_TOML_CONFIGS has no manifest entry for project '$projectName'."
        }
    }
} catch {
    Write-PipelineLog -Message "Invalid VIPM_TOML_CONFIGS repository variable: $($_.Exception.Message) See $manifestDocs" -Level ERROR
    exit 1
}

# ==========================================
# ENTERPRISE PIPELINE ENVIRONMENT AUDIT
# ==========================================
$failures = 0
$warnings = 0

$MinRequired = @{
    PowerShell = [Version]"7.0.0"
    Subversion = [Version]"1.14.0"
    Git        = [Version]"2.40.0"
    VIPM       = [Version]"26.3.2"
    Grype      = [Version]"0.110.0"
}

Write-Host "`n==================================================" -ForegroundColor Cyan
Write-Host "Enterprise Pipeline Environment & Version Audit" -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan

Write-Host -NoNewline "Checking PowerShell Version (>= $($MinRequired.PowerShell))... "
$currentPs =$PSVersionTable.PSVersion
if ($currentPs -ge $MinRequired.PowerShell) {
    Write-Host "[OK] (v$currentPs)" -ForegroundColor Green
} else {
    Write-Host "[FAIL] Found v$currentPs. Minimum required is v$($MinRequired.PowerShell)." -ForegroundColor Red
    $failures++
}

function Test-CliVersionBaseline {
    param([string]$Name, [string]$CommandName, [string]$VersionArg = "--version", [Version]$MinVersion)
    Write-Host -NoNewline "Checking $Name ($CommandName)... "
    $cmd = Get-Command $CommandName -ErrorAction SilentlyContinue
    if ($null -eq $cmd) {
        Write-Host "[FAIL] Not found in system PATH." -ForegroundColor Red
        $script:failures++
        return
    }

    try {
        $rawOutput = & $CommandName $VersionArg 2>&1 | Select-Object -First 1
        if ($rawOutput -match '(\d+\.\d+\.\d+)') {
            $parsedVersion = [Version]$Matches[1]
            if ($parsedVersion -ge $MinVersion) {
                Write-Host "[OK] v$parsedVersion (Min: v$MinVersion)" -ForegroundColor Green
            } else {
                Write-Host "[WARN] v$parsedVersion is below enterprise baseline v$MinVersion." -ForegroundColor Yellow
                $script:warnings++
            }
        } else {
            Write-Host "[OK] Found at $($cmd.Source) (Could not parse semantic version)." -ForegroundColor Yellow
        }
    } catch {
        Write-Host "[OK] Found, but version extraction failed." -ForegroundColor Yellow
    }
}

Test-CliVersionBaseline -Name "Subversion CLI" -CommandName "svn" -MinVersion $MinRequired.Subversion
Test-CliVersionBaseline -Name "Git CLI" -CommandName "git" -MinVersion $MinRequired.Git
Test-CliVersionBaseline -Name "VIPM CLI" -CommandName "vipm" -MinVersion $MinRequired.VIPM
Test-CliVersionBaseline -Name "Grype Scanner" -CommandName "grype" -MinVersion $MinRequired.Grype

Write-Host -NoNewline "Checking DEPENDENCY_TRACK_API_KEY... "
$dependencyTrackMode = if ([string]::IsNullOrWhiteSpace($env:DEPENDENCY_TRACK_MODE)) { "full" } else { $env:DEPENDENCY_TRACK_MODE }
# No built-in default -- Test-DependencyTrackReadiness already fails closed
# with a clear "requires an absolute HTTPS DEPENDENCY_TRACK_URL" error for
# "full" mode when this is unset, rather than silently trying to reach any
# particular organization's private Dependency-Track server.
$dependencyTrackUrl = $env:DEPENDENCY_TRACK_URL
$dependencyTrackMode = Test-DependencyTrackReadiness -Mode $dependencyTrackMode -BaseUrl $dependencyTrackUrl `
    -ApiKey $env:DEPENDENCY_TRACK_API_KEY -SecurityGate $(if ($env:DEPENDENCY_TRACK_SECURITY_GATE) { $env:DEPENDENCY_TRACK_SECURITY_GATE } else { "1" })
$env:DEPENDENCY_TRACK_MODE = $dependencyTrackMode
Write-Host "Dependency-Track reporting policy: $dependencyTrackMode" -ForegroundColor Green

Write-Host "--------------------------------------------------" -ForegroundColor Cyan
if ($failures -gt 0) {
    Write-PipelineLog -Message "Environment Audit FAILED with $failures error(s). Pipeline HALTED." -Level ERROR
    exit 1
}

$configDir = [string]$WorkspaceRoot

function Resolve-Value {
    param([string]$Val)
    if ([string]::IsNullOrEmpty($Val)) { return $Val }
    $trimmed =$Val.Trim()
    if ($trimmed -match '^\$env:(.+)$') { return [Environment]::GetEnvironmentVariable($Matches[1]) }
    return $trimmed
}

function Resolve-AbsolutePath {
    param([string]$PathValue, [string]$BaseDir)
    if ([string]::IsNullOrEmpty($PathValue)) { return $BaseDir }
    $cleanPath =$PathValue.Trim()
    if (-not [System.IO.Path]::IsPathRooted($cleanPath)) {
        return [System.IO.Path]::GetFullPath([System.IO.Path]::Combine($BaseDir,$cleanPath))
    }
    return [System.IO.Path]::GetFullPath($cleanPath)
}

function ConvertFrom-TomlScalar {
    param([string]$Value)
    $valueText = $Value.Trim()
    if ($valueText.StartsWith('"')) {
        return ConvertFrom-Json -InputObject $valueText
    }
    if ($valueText.StartsWith("'") -and $valueText.EndsWith("'")) {
        return $valueText.Substring(1, $valueText.Length - 2)
    }
    return $valueText
}

function Get-TomlLineWithoutComment {
    param([string]$Line)

    $quote = [char]0
    $escaped = $false
    for ($index = 0; $index -lt $Line.Length; $index++) {
        $character = $Line[$index]
        if ($quote -ne [char]0) {
            if ($quote -eq [char]34 -and $character -eq [char]92 -and -not $escaped) {
                $escaped = $true
                continue
            }
            if ($character -eq $quote -and -not $escaped) {
                $quote = [char]0
            }
            $escaped = $false
            continue
        }
        if ($character -eq [char]34 -or $character -eq [char]39) {
            $quote = $character
        } elseif ($character -eq [char]35) {
            return $Line.Substring(0, $index)
        }
    }
    return $Line
}

# ==========================================
# VIPM CLI INVOCATION (exit codes, progress, overrides)
# ==========================================
$script:vipmExitCodes = @{}
$vipmExitCodesPath = [System.IO.Path]::Combine($PSScriptRoot, "vipm-exit-codes.json")
if (Test-Path -LiteralPath $vipmExitCodesPath -PathType Leaf) {
    $script:vipmExitCodes = ConvertFrom-Json -InputObject ([System.IO.File]::ReadAllText($vipmExitCodesPath)) -AsHashtable
}

function Get-VipmFailureMessage {
    param([string]$Command, [int]$ExitCode, [string[]]$ErrorLines)
    $entry = $script:vipmExitCodes[[string]$ExitCode]
    $message = "vipm $Command failed with exit code $ExitCode"
    if ($entry) { $message += " ($($entry.name))" }
    $details = @($ErrorLines | Where-Object { $_ } | Select-Object -First 6 | ForEach-Object { $_.Trim().TrimEnd('.') })
    if ($details.Count -gt 0) { $message += ": " + ($details -join "; ") }
    $message += "."
    if ($entry -and $entry.hint) { $message += " Hint: $($entry.hint)" }
    return $message
}

function Invoke-Vipm {
    <#
    Runs one vipm command with colorless output, classifies its output lines
    (error:/help:/warning: and [VIPM] status lines) and condenses the periodic
    "[VIPM] NN.N%" progress lines that --show-progress emits outside a TTY into
    25% steps. A non-zero exit throws with the documented exit-code name and a
    remediation hint. With -Json, stdout is returned instead of logged.
    #>
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [string]$WorkingDirectory,
        [switch]$ShowProgress,
        [switch]$Json
    )
    $command = $Arguments[0]
    $vipmArgs = @($Arguments) + @("--color-mode", "never")
    if ($ShowProgress) { $vipmArgs += "--show-progress" }
    if ($Json) { $vipmArgs += "--json" }
    Write-PipelineLog -Message "VIPM CMD: vipm $($vipmArgs -join ' ')" -Level TRACE

    $progress = @{ Step = 1; Last = -1.0; Bucket = -1 }
    $errorState = @{ Lines = [System.Collections.Generic.List[string]]::new(); InError = $false }
    $stdout = [System.Collections.Generic.List[string]]::new()
    $stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
    $vipmPath = (Get-Command vipm -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    $workDir = if ($WorkingDirectory) { $WorkingDirectory } else { (Get-Location).ProviderPath }
    $exitCode = Invoke-StreamingProcess -FilePath $vipmPath -ArgumentList $vipmArgs -WorkingDirectory $workDir -OnLine {
            param([string]$rawLine, [bool]$isStderr)
            if ($env:VIPM_DEBUG -eq "1" -and $env:VIPM_DEBUG_LOG) {
                [System.IO.File]::AppendAllText(
                    $env:VIPM_DEBUG_LOG,
                    $rawLine + [Environment]::NewLine,
                    [System.Text.UTF8Encoding]::new($false)
                )
            }
            $line = $rawLine.TrimEnd()
            if ($Json -and -not $isStderr) { $stdout.Add($line); return }
            if ([string]::IsNullOrWhiteSpace($line)) { return }
            if ($line -match '^\[VIPM\]\s+(\d+(?:\.\d+)?)%$') {
                $percent = [double]$Matches[1]
                if ($percent + 5 -lt $progress.Last) { $progress.Step++; $progress.Bucket = -1 }
                $progress.Last = $percent
                $bucket = [int][Math]::Floor([Math]::Min($percent, 100) / 25)
                if ($bucket -ne $progress.Bucket) {
                    $progress.Bucket = $bucket
                    Write-PipelineLog -Message ("  vipm {0}: step {1} at {2:N0}%" -f $command, $progress.Step, $percent) -Level INFO
                }
            } elseif ($line -match '^\s*error:\s*(.*)$') {
                $errorState.InError = $true
                $errorState.Lines.Add($Matches[1])
                Write-PipelineLog -Message "  vipm $command | $line" -Level ERROR
            } elseif ($errorState.InError -and $line -match '^\s+\S') {
                # Indented lines after "error:" itemize it (for example each drifted package).
                $errorState.Lines.Add($line)
                Write-PipelineLog -Message "  vipm $command | $line" -Level ERROR
            } elseif ($line -match '^\s*(help|hint|warning|warn):') {
                $errorState.InError = $false
                Write-PipelineLog -Message "  vipm $command | $line" -Level WARN
            } elseif ($line -match '^\[VIPM\]\s+(.*)$') {
                Write-PipelineLog -Message "  vipm ${command}: $($Matches[1])" -Level INFO
            } else {
                $errorState.InError = $false
                Write-PipelineLog -Message "  vipm $command | $line" -Level INFO
            }
    }
    $stopwatch.Stop()
    Write-PipelineLog -Message ("vipm {0} finished with exit code {1} in {2:N1}s." -f $command, $exitCode, $stopwatch.Elapsed.TotalSeconds) -Level DEBUG
    if ($exitCode -ne 0) {
        throw (Get-VipmFailureMessage -Command $command -ExitCode $exitCode -ErrorLines $errorState.Lines.ToArray())
    }
    if ($Json) { return ($stdout -join [Environment]::NewLine) }
}

function Write-VipmWorkspaceOverride {
    # .vipm/config.toml outranks vipm.toml for every vipm command run in the project,
    # so it has to exist before the first vipm call.
    param([string]$ProjectDir, [string]$LabviewVersion, [string]$LabviewBitness)
    $overrideDir = [System.IO.Path]::Combine($ProjectDir, ".vipm")
    $null = New-Item -ItemType Directory -Force -Path $overrideDir
    $overridePath = [System.IO.Path]::Combine($overrideDir, "config.toml")
    $text = "[project]`nlabview-version = `"$LabviewVersion`"`n"
    if ($LabviewBitness) { $text += "labview-bitness = $LabviewBitness`n" }
    [System.IO.File]::WriteAllText($overridePath, $text, [System.Text.UTF8Encoding]::new($false))
    $bitnessText = if ($LabviewBitness) { " ($LabviewBitness-bit)" } else { "" }
    Write-PipelineLog -Message "Wrote LabVIEW override $LabviewVersion$bitnessText to $overridePath." -Level SUCCESS
}

function Resolve-ProjectVipcPath {
    param([string]$ProjectDir, [string]$RelativePath)
    if ([System.IO.Path]::IsPathRooted($RelativePath) -or ($RelativePath -replace '\\', '/').Split('/') -contains "..") {
        throw "Configuration Error: vipcPath '$RelativePath' must be a path relative to the checkout root."
    }
    $fullPath = [System.IO.Path]::GetFullPath([System.IO.Path]::Combine($ProjectDir, $RelativePath))
    if (-not (Test-Path -LiteralPath $fullPath -PathType Leaf)) {
        throw "Configuration Error: vipcPath '$RelativePath' was not found in the checkout ('$fullPath')."
    }
    if ([System.IO.Path]::GetExtension($fullPath) -ne ".vipc") {
        throw "Configuration Error: vipcPath '$RelativePath' is not a .vipc file."
    }
    return $fullPath
}

function Install-ProjectVipc {
    <#
    Applies the configured .vipc with `vipm install` and then compares the
    configuration's package list with what is installed, because VIPM can
    report success for a .vipc while skipping a package that failed to install.
    Returns the shortfall as "name version (installed: x)" strings.
    #>
    param([string]$ProjectDir, [string]$VipcPath, [string]$LabviewVersion, [string]$LabviewBitness, [string]$QueryDir)
    $expected = [ordered]@{}
    $resolvedPath = Resolve-ProjectVipcPath -ProjectDir $ProjectDir -RelativePath $VipcPath
    $listing = ConvertFrom-Json -InputObject (Invoke-Vipm -Arguments @("list", $resolvedPath) -Json -WorkingDirectory $ProjectDir)
    $packages = @($listing.packages | Where-Object { $_ })
    foreach ($package in $packages) { $expected[[string]$package.name] = [string]$package.version }
    Write-PipelineLog -Message "Installing VI Package Configuration '$VipcPath' ($($packages.Count) packages) into LabVIEW $LabviewVersion ($LabviewBitness-bit)..." -Level INFO
    Invoke-Vipm -Arguments @("install", $resolvedPath, "-y") -WorkingDirectory $ProjectDir -ShowProgress

    # Listing from outside the project returns every installed package, not only vipm.toml's.
    $installed = ConvertFrom-Json -InputObject (Invoke-Vipm -Arguments @(
            "list", "--installed", "--labview-version", $LabviewVersion, "--labview-bitness", $LabviewBitness
        ) -Json -WorkingDirectory $QueryDir)
    $installedVersions = @{}
    foreach ($package in @($installed.dependencies | Where-Object { $_ })) {
        $installedVersions[[string]$package.name] = [string]$package.installed_version
    }
    $missing = @(foreach ($name in $expected.Keys) {
            if ($installedVersions[$name] -ne $expected[$name]) {
                $have = if ($installedVersions[$name]) { $installedVersions[$name] } else { "none" }
                "$name $($expected[$name]) (installed: $have)"
            }
        })
    if ($missing.Count -eq 0) {
        Write-PipelineLog -Message "Verified all $($expected.Count) .vipc package(s) are installed in LabVIEW $LabviewVersion ($LabviewBitness-bit)." -Level SUCCESS
    } else {
        Write-PipelineLog -Message "vipm install reported success, but $($missing.Count) of $($expected.Count) .vipc package(s) are not installed (a package-level install action probably failed; install the .vip on its own to see the error):" -Level WARN
        foreach ($item in $missing) { Write-PipelineLog -Message "  not installed: $item" -Level WARN }
    }
    return ,$missing
}

function Read-VipmLockData {
    param([string]$Path)

    $packages = [System.Collections.Generic.List[object]]::new()
    $package = $null
    foreach ($line in (Get-Content -LiteralPath $Path)) {
        $line = Get-TomlLineWithoutComment -Line $line
        if ($line -match '^\s*\[\[package\]\]\s*$') {
            if ($null -ne $package) { $packages.Add([PSCustomObject]$package) }
            $package = @{}
            continue
        }
        if ($null -ne $package -and $line -match '^\s*(name|version|manager)\s*=\s*(.*?)\s*$') {
            $package[$Matches[1]] = ConvertFrom-TomlScalar -Value $Matches[2]
        }
    }
    if ($null -ne $package) { $packages.Add([PSCustomObject]$package) }
    return $packages.ToArray()
}

$script:nipkgPath = (Get-Command nipkg.exe -ErrorAction SilentlyContinue).Source
if (-not $script:nipkgPath) {$script:nipkgPath = "C:\Program Files\National Instruments\NI Package Manager\nipkg.exe"
}
Write-PipelineLog -Message "Resolved NIPM Executable Path: $($script:nipkgPath)" -Level TRACE

$script:nipmGraph = @{}
$script:nipmComponents = @{}

function Resolve-NipmDependencyGraph {
    param([string]$PkgName)
    
    $parentKey =$PkgName.ToLower()
    if ($script:nipmGraph.ContainsKey($parentKey)) { return }
    $script:nipmGraph[$parentKey] = [System.Collections.Generic.List[string]]::new()

    if (-not (Test-Path $script:nipkgPath)) { return }
    Write-PipelineLog -Message "  [NIPM] Scanning dependency node: $PkgName..." -Level TRACE
    
    $nipkgArgs = @("get-dependencies", $PkgName)
    $output = &$script:nipkgPath @nipkgArgs 2>&1

    foreach ($line in ($output -split "`r?`n")) {
        $strLine = [string]$line
        if ($strLine -match '^\s*([^\s]+)\s+([^\s]+)\s+(windows_[^\s]+)\s+(.*)$') {
            $childName =$Matches[1]
            $childVer =$Matches[2]
            $childDesc =$Matches[4].Trim()
            $childKey =$childName.ToLower()
            
            if ($childKey -eq $parentKey) { continue }
            $script:nipmGraph[$parentKey].Add($childKey) | Out-Null
            
            if (-not $script:nipmComponents.ContainsKey($childKey)) {
                Write-PipelineLog -Message "    + Discovered recursive dependency: $childName v$childVer" -Level TRACE
                $safeVer = $childVer -replace '\+', '%2B'
                $childPurl = "pkg:nipkg/$childName@$safeVer"
                
                $script:nipmComponents[$childKey] = [PSCustomObject]@{
                    "bom-ref"   = $childPurl
                    type        = "library"
                    group       = "nipm"
                    name        = $childName
                    version     = $childVer
                    description = $childDesc
                    purl        = $childPurl
                    supplier    = [PSCustomObject]@{ name = "National Instruments" }
                }
            }
            Resolve-NipmDependencyGraph -PkgName $childName
        }
    }
}

function Add-NipmLockProperty {
    # Records the vipm.lock version on an installed NIPM component and flags drift.
    param($Component, $Package)
    $lockProperties = @(@($Component.properties) | Where-Object { $_ -and $_.name -notin "vipm:locked-version", "vipm:version-drift" })
    $lockProperties += [PSCustomObject]@{ name = "vipm:locked-version"; value = [string]$Package.version }
    if ([string]$Component.version -ne [string]$Package.version) {
        $lockProperties += [PSCustomObject]@{ name = "vipm:version-drift"; value = "true" }
    }
    $Component | Add-Member -NotePropertyName "type" -NotePropertyValue "library" -Force
    $Component | Add-Member -NotePropertyName "group" -NotePropertyValue ([string]$Package.manager) -Force
    $Component | Add-Member -NotePropertyName "properties" -NotePropertyValue $lockProperties -Force
}

function ConvertTo-LockComponent {
    param($Package)
    $packagePurl = "pkg:generic/$([uri]::EscapeDataString([string]$Package.name))@$([uri]::EscapeDataString([string]$Package.version))"
    return [PSCustomObject]@{
        "bom-ref" = $packagePurl
        type = "library"
        group = [string]$Package.manager
        name = [string]$Package.name
        version = [string]$Package.version
        purl = $packagePurl
    }
}

function Add-PendingNipmComponent {
    # Locked NIPM packages the SBOM lacks: annotate the nipkg-resolved component, or add a pkg:generic one if not installed.
    param($Pending, $ComponentsList, [hashtable]$NameToBomRef)
    foreach ($package in $Pending) {
        $key = ([string]$package.name).ToLower()
        $component = $null
        if ($NameToBomRef.ContainsKey($key)) {
            $component = $ComponentsList | Where-Object { $_."bom-ref" -eq $NameToBomRef[$key] } | Select-Object -First 1
        }
        if ($null -ne $component) {
            Add-NipmLockProperty -Component $component -Package $package
        } else {
            $component = ConvertTo-LockComponent -Package $package
            $ComponentsList.Add($component)
            $NameToBomRef[$key] = $component."bom-ref"
        }
    }
}

# ==========================================
# GLOBAL CONFIGURATION PARSING
# ==========================================
$baseUrls = @{}
if ($null -ne $config.baseUrls) {
    if ($null -ne $config.baseUrls.svn) { $baseUrls['svn'] = ([string]$config.baseUrls.svn).Trim().TrimEnd('/') + '/' }
    if ($null -ne $config.baseUrls.git) { $baseUrls['git'] = ([string]$config.baseUrls.git).Trim().TrimEnd('/') + '/' }
}

$rawWorkingDir = if ($config.workingDirectoryBase) { [string]$config.workingDirectoryBase } else { "workspace" }
$workingDirBase = Resolve-AbsolutePath -PathValue $rawWorkingDir -BaseDir $configDir

$globalUsername = Resolve-Value ([string]$config.username)
$globalPassword = Resolve-Value ([string]$config.password)
$globalToken = Resolve-Value ([string]$config.token)
$cleanOutputs = if ($null -ne $config.cleanOutputsBeforeRun) { [bool]$config.cleanOutputsBeforeRun } else { $true }

$outputBaseDir = Resolve-AbsolutePath -PathValue "output" -BaseDir $configDir
if ($cleanOutputs -and [System.IO.Directory]::Exists($outputBaseDir)) {
    Write-PipelineLog -Message "Cleaning global output directory '$outputBaseDir'..." -Level DEBUG
    Remove-Item -Path "$outputBaseDir\*" -Recurse -Force -ErrorAction SilentlyContinue
} elseif (-not [System.IO.Directory]::Exists($outputBaseDir)) {
    $null = New-Item -ItemType Directory -Force -Path $outputBaseDir
}

$summaryResults = [System.Collections.Generic.List[PSCustomObject]]::new()
$projIndex = 1
$totalProjects = $config.projects.Count

foreach ($proj in $config.projects) {
    $projectStopwatch = [System.Diagnostics.Stopwatch]::StartNew()
    
    $projectName = [string]$proj.name
    if ($null -ne $projectName) { $projectName = $projectName.Trim() }
    $isEnabled = if ($null -ne $proj.enabled) { [bool]$proj.enabled } else { $true }
    $displayName = if ($proj.displayName) { [string]$proj.displayName } else { $projectName }
    if ($null -ne $displayName) { $displayName = $displayName.Trim() }

    Write-PipelineLog -Message "`n==================================================" -Level INFO
    Write-PipelineLog -Message "Project [$projIndex/$totalProjects]: $projectName (Display: $displayName)" -Level INFO
    Write-PipelineLog -Message "==================================================" -Level INFO

    if (-not $isEnabled) {
        Write-PipelineLog -Message "Project '$projectName' is disabled. Skipping..." -Level WARN
        $summaryResults.Add([PSCustomObject]@{ ProjectName = $projectName; Status = "SKIPPED"; Details = "Disabled in config." })
        $projIndex++
        continue
    }

    $runRecord = New-PipelineRunRecord -Project $projectName -Pipeline "native"
    $runRecord.reportingMode = $dependencyTrackMode
    try {
        # ==========================================
        # PROJECT-LEVEL VALIDATION & URL MAPPING
        # ==========================================
        $vcsType = if ($proj.vcs) { ([string]$proj.vcs).Trim().ToLower() } else { "" }
        if ([string]::IsNullOrEmpty($vcsType) -or ($vcsType -ne "svn" -and $vcsType -ne "git")) {
            throw "Configuration Error: Project '$projectName' must explicitly define a valid 'vcs' type ('git' or 'svn')."
        }

        if ($proj.repoUrl) {
            $repoUrl = ([string]$proj.repoUrl).Trim()
        } elseif ($baseUrls.ContainsKey($vcsType)) {
            $repoUrl = "$($baseUrls[$vcsType])$projectName"
        } else {
            throw "Configuration Error: Project '$projectName' uses VCS '$vcsType', but no base URL is defined for '$vcsType' in the global 'baseUrls' object and no explicit 'repoUrl' was provided."
        }

        $revision = if ($proj.revision) { ([string]$proj.revision).Trim() } else { "HEAD" }
        $sparsePaths = @($proj.sparsePaths | Where-Object { $_ } | ForEach-Object { ([string]$_).Trim() })
        if ($sparsePaths.Count -gt 0 -and $vcsType -ne 'svn') {
            throw "Configuration Error: Project '$projectName' sets sparsePaths, which only applies to SVN checkouts."
        }
        $patchPaths = @($proj.patches | Where-Object { $_ } | ForEach-Object { ([string]$_).Trim() })
        $buildPackages = $false
        $signedBuild = $null
        if ($null -ne $proj.build) {
            $buildPackages = $proj.build.packages -eq $true
            $hasSbomPath = -not [string]::IsNullOrWhiteSpace([string]$proj.build.sbomPath)
            $hasArtifactPath = -not [string]::IsNullOrWhiteSpace([string]$proj.build.artifactPath)
            if ($hasSbomPath -ne $hasArtifactPath) {
                throw "Configuration Error: Project '$projectName' build.sbomPath and build.artifactPath must be set together."
            }
            if ($hasSbomPath) { $signedBuild = @{ sbomPath = [string]$proj.build.sbomPath; artifactPath = [string]$proj.build.artifactPath } }
            if (-not $buildPackages -and $null -eq $signedBuild) {
                throw "Configuration Error: Project '$projectName' build must set packages = true and/or sbomPath with artifactPath."
            }
        }
        
        $username = if ($proj.username) { Resolve-Value ([string]$proj.username) } else { $globalUsername }
        $password = if ($proj.password) { Resolve-Value ([string]$proj.password) } else { $globalPassword }
        $token = if ($proj.token) { Resolve-Value ([string]$proj.token) } else { $globalToken }
        $secret = if (-not [string]::IsNullOrEmpty($token)) { $token } else { $password }

        $projectWorkingDir = [System.IO.Path]::Combine($workingDirBase, $projectName)
        Set-PipelineStage -Record $runRecord -Name "checkout" -Status running

        Invoke-PipelineCheckoutStage -ProjectWorkingDir $projectWorkingDir -VcsType $vcsType -RepoUrl $repoUrl `
            -Revision $revision -Username $username -Secret $secret -SparsePaths $sparsePaths -ResolvedRevision ([ref]$revision)

        $appliedPatches = @()
        if ($patchPaths.Count -gt 0) {
            $appliedPatches = @(Invoke-CheckoutPatch -ProjectDir $projectWorkingDir -Vcs $vcsType -PatchPaths $patchPaths -BaseDir $WorkspaceRoot)
        }
        $runRecord.source = [ordered]@{ vcs = $vcsType; url = $repoUrl; revision = $revision }
        $runRecord.patches = @($appliedPatches | ForEach-Object { [ordered]@{ path = $_.Path; sha256 = $_.Sha256 } })
        Set-PipelineStage -Record $runRecord -Name "checkout" -Status completed
        $sourceCheckouts = @($proj.sourceCheckouts | Where-Object { $null -ne $_ })
        if ($sourceCheckouts.Count -gt 0) {
            Set-PipelineStage -Record $runRecord -Name "sourceCheckouts" -Status running
            $runRecord.sourceCheckouts = @(Invoke-PipelineSourceCheckoutStage -ProjectWorkingDir $projectWorkingDir -SourceCheckouts $sourceCheckouts)
            Set-PipelineStage -Record $runRecord -Name "sourceCheckouts" -Status completed
        } else {
            Set-PipelineStage -Record $runRecord -Name "sourceCheckouts" -Status skipped -Reason "No additional source repositories are configured."
        }

        $lvlibpDependencies = @($proj.build.lvlibpDependencies | Where-Object { $null -ne $_ })
        if ($lvlibpDependencies.Count -gt 0) {
            Set-PipelineStage -Record $runRecord -Name "releaseDependencies" -Status running
            $runRecord.releaseDependencies = @(Invoke-LvlibpReleaseDependencyStage -Dependencies $lvlibpDependencies -WorkspaceRoot $WorkspaceRoot)
            Set-PipelineStage -Record $runRecord -Name "releaseDependencies" -Status completed
        } else {
            Set-PipelineStage -Record $runRecord -Name "releaseDependencies" -Status skipped -Reason "No immutable LVLIBP dependencies are configured."
        }

        $projectSubOutputDir = [System.IO.Path]::Combine($outputBaseDir, $projectName)
        if (-not [System.IO.Directory]::Exists($projectSubOutputDir)) {
            $null = New-Item -ItemType Directory -Force -Path $projectSubOutputDir
        }

        if (-not $vipmManifestsByProject.Contains($projectName)) {
            throw "Configuration Error: VIPM_TOML_CONFIGS has no manifest entry for project '$projectName'. See $manifestDocs"
        }
        $vipmTomlContent = $vipmManifestsByProject[$projectName]
        if ($vipmTomlContent -isnot [string] -or [string]::IsNullOrWhiteSpace($vipmTomlContent)) {
            throw "Configuration Error: VIPM_TOML_CONFIGS entry for project '$projectName' must be a non-blank TOML string. See $manifestDocs"
        }
        $tomlPath = [System.IO.Path]::Combine($projectWorkingDir, "vipm.toml")
        $lvOverrideVersion = if ($proj.labviewVersion) { ([string]$proj.labviewVersion).Trim() } else { "" }
        $lvOverrideBitness = if ($proj.labviewBitness) { ([string]$proj.labviewBitness).Trim() } else { "" }
        $vipcPath = ([string]$proj.vipcPath).Trim()
        Invoke-PipelineManifestStage -ProjectWorkingDir $projectWorkingDir -VipmTomlContent $vipmTomlContent `
            -LvOverrideVersion $lvOverrideVersion -LvOverrideBitness $lvOverrideBitness

        $tomlFiles = @(Get-Item -LiteralPath $tomlPath)
        $processedManifests = 0

        foreach ($tomlFile in $tomlFiles) {
                $tomlPath = $tomlFile.FullName
                $projectDir = [System.IO.Path]::GetDirectoryName($tomlPath)
                $projectRelativeName = [System.IO.Path]::GetFileName($projectDir)
                
                Write-PipelineLog -Message "Discovered LabVIEW project manifest: $tomlPath" -Level SUCCESS

                $tomlSection = ""
                $projectTomlValues = @{}
                $rootTomlValues = @{}
                foreach ($line in (Get-Content $tomlPath)) {
                    $line = Get-TomlLineWithoutComment -Line $line
                    if ($line -match '^\s*\[([^\]]+)\]\s*$') {
                        $tomlSection = $Matches[1].Trim()
                    } elseif ($line -match '^\s*([A-Za-z0-9_-]+)\s*=\s*(.*?)\s*$') {
                        $tomlKey = $Matches[1]
                        $tomlValue = ConvertFrom-TomlScalar -Value $Matches[2]
                        if ($tomlSection -eq "project") {
                            $projectTomlValues[$tomlKey] = $tomlValue
                        } elseif ([string]::IsNullOrEmpty($tomlSection)) {
                            $rootTomlValues[$tomlKey] = $tomlValue
                        }
                    }
                }

                $effectiveLvBitness = $projectTomlValues['labview-bitness']
                if ([string]::IsNullOrWhiteSpace([string]$effectiveLvBitness)) {
                    $effectiveLvBitness = $rootTomlValues['labview-bitness']
                }
                if ([string]::IsNullOrWhiteSpace([string]$effectiveLvBitness)) {
                    $effectiveLvBitness = "64"
                }

                $effectiveLvVersion = "2026"
                if (-not [string]::IsNullOrWhiteSpace([string]$projectTomlValues['labview-version'])) {
                    $effectiveLvVersion = [string]$projectTomlValues['labview-version']
                } elseif (-not [string]::IsNullOrWhiteSpace([string]$rootTomlValues['labview-version'])) {
                    $effectiveLvVersion = [string]$rootTomlValues['labview-version']
                }

                Write-PipelineLog -Message "Resolved Target Environment from TOML: LabVIEW $effectiveLvVersion ($effectiveLvBitness-bit)" -Level INFO
                $requestedLv = "$effectiveLvVersion ($effectiveLvBitness-bit)"
                if ($lvOverrideVersion) {
                    $effectiveLvVersion = $lvOverrideVersion
                    if ($lvOverrideBitness) { $effectiveLvBitness = $lvOverrideBitness }
                    Write-PipelineLog -Message "Project override (.vipm/config.toml) selects LabVIEW $effectiveLvVersion ($effectiveLvBitness-bit) for every vipm command." -Level INFO
                }
                $usedLv = "$effectiveLvVersion ($effectiveLvBitness-bit)"

                $vipcShortfall = @()
                if ($vipcPath) {
                    $vipcShortfall = Install-ProjectVipc -ProjectDir $projectDir -VipcPath $vipcPath `
                        -LabviewVersion $effectiveLvVersion -LabviewBitness $effectiveLvBitness -QueryDir $projectSubOutputDir
                }

                $packageReports = @()
                if ($buildPackages) {
                    Set-PipelineStage -Record $runRecord -Name "packages" -Status running
                    $packagePlan = Resolve-PackageBuildPlan -TomlPath $tomlPath -ProjectDir $projectDir
                    Write-PipelineLog -Message "Building VI packages in dependency order: $(($packagePlan | ForEach-Object Target) -join ' -> ')." -Level INFO
                    $packageReports = @(Invoke-PackageBuild -Plan $packagePlan -ProjectDir $projectDir -OutputDir $projectSubOutputDir `
                        -QueryDir $projectSubOutputDir -LabviewVersion $effectiveLvVersion -LabviewBitness $effectiveLvBitness `
                        -AllowPackageDrift ($env:ALLOW_PACKAGE_DRIFT -eq "1") -RetryVipmLibraryAddStall)
                    Set-PipelineStage -Record $runRecord -Name "packages" -Status completed
                } else {
                    Set-PipelineStage -Record $runRecord -Name "packages" -Status skipped -Reason "build.packages is not enabled."
                }
                Set-PipelineStage -Record $runRecord -Name "sbom" -Status running

                $lockPath = [System.IO.Path]::Combine($projectDir, "vipm.lock")
                Write-PipelineLog -Message "Generating vipm.lock from the deployed manifest..." -Level INFO
                Invoke-Vipm -Arguments @("lock") -WorkingDirectory $projectDir
                if (-not (Test-Path -LiteralPath $lockPath -PathType Leaf)) {
                    throw "VIPM lock generation completed without creating '$lockPath'."
                }
                $lockPackages = @(Read-VipmLockData -Path $lockPath)
                $buildReport = $null
                if ($null -ne $signedBuild) {
                    Write-PipelineLog -Message "Building all VIPM targets and verifying Authenticode..." -Level INFO
                    $buildReport = Invoke-ReportedVipmBuild -Build $signedBuild -ProjectDir $projectDir -OutputDir $projectSubOutputDir `
                        -LabviewVersion $effectiveLvVersion -LabviewBitness $effectiveLvBitness `
                        -AllowPackageDrift ($env:ALLOW_PACKAGE_DRIFT -eq "1")
                }

                $DtProjectName = "$displayName`:$projectRelativeName"
                $ProductVersion = "1.0.0"
                if (-not [string]::IsNullOrWhiteSpace([string]$projectTomlValues['version'])) {
                    $ProductVersion = [string]$projectTomlValues['version']
                }
                
                $safeDisplayName =$displayName -replace '[^a-zA-Z0-9\-]', '_'
                $sbomPath = [System.IO.Path]::Combine($projectSubOutputDir, "$safeDisplayName-$projectRelativeName-sbom.json")
                
                Invoke-PipelineSbomStage -TomlPath $tomlPath -ProjectDir $projectDir -ProjectRelativeName $projectRelativeName `
                    -ProductVersion $ProductVersion -SbomPath $sbomPath -LvOverrideVersion $lvOverrideVersion `
                    -EffectiveLvVersion $effectiveLvVersion -EffectiveLvBitness $effectiveLvBitness

                Write-PipelineLog -Message "Parsing local CycloneDX SBOM for Intelligence & Dependency Grouping..." -Level DEBUG
                $sbomContent = Get-Content -LiteralPath $sbomPath -Raw | ConvertFrom-Json -Depth 100

                $buildProperties = [System.Collections.Generic.List[object]]::new()
                                foreach ($property in @($sbomContent.metadata.properties | Where-Object { $_ -and $_.name -notlike "vipm:labview-*" -and $_.name -notlike "vipm:vipc*" })) {
                    $buildProperties.Add($property)
                }
                $buildProperties.Add([PSCustomObject]@{ name = "vipm:labview-requested"; value = $requestedLv })
                $buildProperties.Add([PSCustomObject]@{ name = "vipm:labview-used"; value = $usedLv })
                if ($vipcPath) { $buildProperties.Add([PSCustomObject]@{ name = "vipm:vipc"; value = $vipcPath }) }
                foreach ($item in $vipcShortfall) { $buildProperties.Add([PSCustomObject]@{ name = "vipm:vipc-not-installed"; value = $item }) }
                foreach ($applied in $appliedPatches) { $buildProperties.Add([PSCustomObject]@{ name = "checkout:patch"; value = "$($applied.Path) sha256:$($applied.Sha256)" }) }
                $sbomContent.metadata | Add-Member -NotePropertyName "properties" -NotePropertyValue $buildProperties.ToArray() -Force

                if ($sbomContent.metadata.component.version) {
                    $ProductVersion =$sbomContent.metadata.component.version
                }

                $vipmDeps = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
                $nipmRoots = @{}

                $currentSection = ""
                foreach ($line in (Get-Content -LiteralPath $tomlPath)) {
                    $line =$line.Trim()
                    if ($line -match '^\[(.*)\]$') {
                        $currentSection =$Matches[1]
                    } elseif ($line -match '^([^=]+)=\s*(.*)$') {
                        $pkgName =$Matches[1].Trim(' "''')
                        $pkgVer =$Matches[2].Trim(' "''')
                        if ($currentSection -eq "dependencies") {
                            [void]$vipmDeps.Add($pkgName)
                        } elseif ($currentSection -eq "nipm.dependencies") {
                            $nipmRoots[$pkgName] =$pkgVer
                        }
                    }
                }

                $compNameToBomRef = @{}
                if ($sbomContent.components) {
                    foreach ($comp in $sbomContent.components) {
                        if ($vipmDeps.Contains($comp.name)) {
                            $comp | Add-Member -NotePropertyName "group" -NotePropertyValue "vipm" -Force
                        } elseif ($nipmRoots.ContainsKey($comp.name) -or ($comp.purl -and $comp.purl -match '^pkg:nipkg/')) {
                            $comp | Add-Member -NotePropertyName "group" -NotePropertyValue "nipm" -Force
                        }
                        $compNameToBomRef[$comp.name.ToLower()] =$comp."bom-ref"
                    }
                }

                $componentsList = [System.Collections.Generic.List[object]]::new()
                if ($sbomContent.components) { $componentsList.AddRange($sbomContent.components) }
                $pendingNipm = [System.Collections.Generic.List[object]]::new()
                foreach ($package in $lockPackages) {
                    if ([string]::IsNullOrWhiteSpace([string]$package.name) -or
                        [string]::IsNullOrWhiteSpace([string]$package.version) -or
                        [string]::IsNullOrWhiteSpace([string]$package.manager)) {
                        throw "Generated vipm.lock contains a package missing name, version, or manager."
                    }

                    $component = $null
                    foreach ($candidate in $componentsList) {
                        if ($candidate.name -eq $package.name -and $candidate.version -eq $package.version) {
                            $component = $candidate
                            break
                        }
                    }
                    if ($null -eq $component -and $package.manager -eq "nipm") {
                        # An installed NIPM package can differ from the locked version (drift); keep one component per package.
                        $component = $componentsList | Where-Object { $_.name -eq $package.name -and ([string]$_.purl).StartsWith("pkg:nipkg/") } | Select-Object -First 1
                        if ($null -eq $component) {
                            # Resolved after the nipkg scan so installed packages get pkg:nipkg purls.
                            $pendingNipm.Add($package)
                            continue
                        }
                    }
                    if ($null -eq $component) {
                        $component = ConvertTo-LockComponent -Package $package
                        $componentsList.Add($component)
                    } elseif ($package.manager -eq "nipm") {
                        Add-NipmLockProperty -Component $component -Package $package
                    } else {
                        $component | Add-Member -NotePropertyName "type" -NotePropertyValue "library" -Force
                        $component | Add-Member -NotePropertyName "group" -NotePropertyValue ([string]$package.manager) -Force
                    }
                    $compNameToBomRef[$package.name.ToLower()] = $component."bom-ref"
                }
                $sbomContent.components = $componentsList.ToArray()

                $dynamicToolProperties = @()
                if ($sbomContent.metadata.tools.components) {
                    foreach ($tool in $sbomContent.metadata.tools.components) {
                        $safeToolName = $tool.name -replace '[^a-zA-Z0-9\-_]', ''
                        $dynamicToolProperties += @{ Name = $safeToolName; Value = $tool.version }
                    }
                }

                if ((Test-Path $script:nipkgPath) -and ($nipmRoots.Count -gt 0)) {
                    Write-PipelineLog -Message "Recursively building deep NIPM dependency graph..." -Level INFO
                    $script:nipmGraph.Clear()
                    $script:nipmComponents.Clear()

                    foreach ($nipmName in $nipmRoots.Keys) {
                        Resolve-NipmDependencyGraph -PkgName $nipmName
                    }

                    $componentsList = [System.Collections.Generic.List[object]]::new()
                    if ($sbomContent.components) { $componentsList.AddRange($sbomContent.components) }
                    $newCount = 0
                    foreach ($compKey in $script:nipmComponents.Keys) {
                        if (-not $compNameToBomRef.ContainsKey($compKey)) {$componentsList.Add($script:nipmComponents[$compKey])
                            $compNameToBomRef[$compKey] = $script:nipmComponents[$compKey]."bom-ref"
                            $newCount++
                        }
                    }
                    Add-PendingNipmComponent -Pending $pendingNipm -ComponentsList $componentsList -NameToBomRef $compNameToBomRef
                    $sbomContent.components =$componentsList.ToArray()

                    $depNodes = @{}
                    if ($sbomContent.dependencies) {
                        foreach ($node in $sbomContent.dependencies) {
                            $deps = [System.Collections.Generic.List[string]]::new()
                            if ($node.dependsOn) {
                                foreach ($d in $node.dependsOn) { $deps.Add([string]$d) }
                            }
                            $depNodes[$node.ref] = $deps
                        }
                    }
                    
                    $rootBomRef =$sbomContent.metadata.component."bom-ref"
                    if (-not $depNodes.ContainsKey($rootBomRef)) {
                        $depNodes[$rootBomRef] = [System.Collections.Generic.List[string]]::new()
                    }
                    foreach ($rootName in $nipmRoots.Keys) {
                        $ref = $compNameToBomRef[$rootName.ToLower()]
                        if ($null -ne $ref -and -not $depNodes[$rootBomRef].Contains($ref)) {
                            $depNodes[$rootBomRef].Add($ref)
                        }
                    }

                    foreach ($parentKey in $script:nipmGraph.Keys) {
                        $parentRef = $compNameToBomRef[$parentKey]
                        if ($null -eq $parentRef) { continue }
                        
                        if (-not $depNodes.ContainsKey($parentRef)) {
                            $depNodes[$parentRef] = [System.Collections.Generic.List[string]]::new()
                        }
                        
                        foreach ($childKey in $script:nipmGraph[$parentKey]) {
                            $childRef = $compNameToBomRef[$childKey]
                            if ($null -ne $childRef -and -not $depNodes[$parentRef].Contains($childRef)) {
                                $depNodes[$parentRef].Add($childRef)
                            }
                        }
                    }

                    $finalDependencies = [System.Collections.Generic.List[object]]::new()
                    foreach ($nodeRef in $depNodes.Keys) {$finalDependencies.Add([PSCustomObject]@{
                            ref = $nodeRef
                            dependsOn = $depNodes[$nodeRef].ToArray()
                        })
                    }
                    $sbomContent.dependencies =$finalDependencies.ToArray()
                    
                    Write-PipelineLog -Message "Mapped $newCount new NIPM components and recursive relationships into the Graph." -Level SUCCESS
                } else {
                    Write-PipelineLog -Message "nipkg.exe not found or no [nipm.dependencies] in TOML. Skipping NIPM recursion." -Level WARN
                    $componentsList = [System.Collections.Generic.List[object]]::new()
                    if ($sbomContent.components) { $componentsList.AddRange($sbomContent.components) }
                    Add-PendingNipmComponent -Pending $pendingNipm -ComponentsList $componentsList -NameToBomRef $compNameToBomRef
                    $sbomContent.components = $componentsList.ToArray()
                }

                if ($packageReports.Count -gt 0) {
                    $mergedSbom = ConvertFrom-Json -InputObject ($sbomContent | ConvertTo-Json -Depth 100) -AsHashtable -Depth 100
                    Add-PackageBuildComponent -Sbom $mergedSbom -Reports $packageReports -RepoUrl $repoUrl -Revision $revision
                    $sbomContent = $mergedSbom
                    Write-PipelineLog -Message "Included $($packageReports.Count) built package(s): $(($packageReports | ForEach-Object { "$($_.Name) $($_.Version)" }) -join ', ')." -Level SUCCESS
                }
                if ($null -ne $buildReport) {
                    $mergedSbom = ConvertFrom-Json -InputObject ($sbomContent | ConvertTo-Json -Depth 100) -AsHashtable -Depth 100
                    Add-VerifiedBuildComponent -Sbom $mergedSbom -Report $buildReport
                    $sbomContent = $mergedSbom
                    Write-PipelineLog -Message "Included build '$($buildReport.Bom.metadata.component.name)' with verified Authenticode signature." -Level SUCCESS
                }
                $sbomContent | ConvertTo-Json -Depth 100 -Compress | Set-Content -LiteralPath $sbomPath -Encoding utf8
                Set-PipelineStage -Record $runRecord -Name "sbom" -Status completed

                $grypeReportPath = [System.IO.Path]::Combine($projectSubOutputDir, "$safeDisplayName-$projectRelativeName-vuln-report.txt")
                Invoke-PipelineScanStage -SbomPath $sbomPath -GrypeReportPath $grypeReportPath -Record $runRecord
                
                $dtBaseUrl = $dependencyTrackUrl

                if ($dependencyTrackMode -eq "full") {$ciUrl = if ($env:GITHUB_SERVER_URL) { "$env:GITHUB_SERVER_URL/$env:GITHUB_REPOSITORY/actions/runs/$env:GITHUB_RUN_ID" } elseif ($env:BUILD_URL) {$env:BUILD_URL } else { "Local Build" }

                    $descLines = @(
                        "Revision: $revision",
                        "CI/CD Link: $ciUrl"
                    )

                    $tagSet = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
                    [void]$tagSet.Add("vcs-$vcsType")
                    [void]$tagSet.Add("labview-$effectiveLvVersion")
                    [void]$tagSet.Add("bitness-$effectiveLvBitness")

                    if ($null -ne $dynamicToolProperties) {
                        foreach ($tool in $dynamicToolProperties) {
                            $cleanToolName = $tool.Name -replace '[^a-zA-Z0-9\-_]', ''
                            $descLines += "$($cleanToolName): $($tool.Value)"
                            [void]$tagSet.Add("$($cleanToolName.ToLower())-$($tool.Value)")
                        }
                    }

                    $formattedDescription =$descLines -join " | "
                    $dtExportPath = [System.IO.Path]::Combine($projectSubOutputDir, "$safeDisplayName-$projectRelativeName-dt-findings.json")

                    Set-PipelineStage -Record $runRecord -Name "dependencyTrack" -Status running
                    try {
                        $dtGate = Publish-DependencyTrack -BaseUrl $dtBaseUrl `
                                                -ApiKey $env:DEPENDENCY_TRACK_API_KEY `
                                                -ProjectName $DtProjectName `
                                                -ProjectVersion $ProductVersion `
                                                -SbomPath $sbomPath `
                                                -ExportPath $dtExportPath `
                                                -Description $formattedDescription `
                                                -Tags ($tagSet | Sort-Object) `
                                                -Record $runRecord
                        Set-PipelineStage -Record $runRecord -Name "dependencyTrack" -Status completed -Gate $dtGate
                    } catch {
                        if ($_.Exception.Message -like "SECURITY GATE FAILED*") {
                            Set-PipelineStage -Record $runRecord -Name "dependencyTrack" -Status completed -Gate failed
                        }
                        throw
                    }
                } else {
                    Write-PipelineLog -Message "DEPENDENCY_TRACK_MODE=hosted-grype; skipping Dependency-Track upload and gate." -Level WARN
                    Set-PipelineStage -Record $runRecord -Name "dependencyTrack" -Status skipped -Reason "DEPENDENCY_TRACK_MODE=hosted-grype."
                }
                $processedManifests++
        }

        $projectStopwatch.Stop()
        $elapsed = $projectStopwatch.Elapsed.ToString("hh\:mm\:ss")

        $summaryResults.Add([PSCustomObject]@{
            ProjectName = $projectName
            Status      = "SUCCESS"
            Details     = "Processed $processedManifests LabVIEW manifest(s) in $elapsed."
        })
        Complete-PipelineRunRecord -Record $runRecord
        Write-PipelineRunRecord -Record $runRecord -OutputDir ([System.IO.Path]::Combine($outputBaseDir, $projectName))
        Write-PipelineLog -Message "Project '$projectName' completed successfully!" -Level SUCCESS

    } catch {
        $errorMessage = $_.Exception.Message
        Write-PipelineLog -Message "FATAL ERROR in execution loop for $projectName : $errorMessage" -Level ERROR
        Complete-PipelineRunRecord -Record $runRecord -Failure $errorMessage
        try { Write-PipelineRunRecord -Record $runRecord -OutputDir ([System.IO.Path]::Combine($outputBaseDir, $projectName)) }
        catch { Write-PipelineLog -Message "Could not write pipeline-run.json for $projectName : $($_.Exception.Message)" -Level ERROR }

        $projectStopwatch.Stop()
        $summaryResults.Add([PSCustomObject]@{
            ProjectName = $projectName
            Status      = "FAILED"
            Details     = "Failed after $($projectStopwatch.Elapsed.ToString("hh\:mm\:ss")): $errorMessage"
        })
    }
    $projIndex++
}

Write-PipelineLog -Message "`n==================================================" -Level INFO
Write-PipelineLog -Message "BATCH EXECUTION SUMMARY REPORT" -Level INFO
Write-PipelineLog -Message "==================================================" -Level INFO

$counts = @{ Succeeded = 0; Failed = 0; Skipped = 0 }
for ($i = 0; $i -lt $summaryResults.Count; $i++) {$res = $summaryResults[$i]
    if ($res.Status -eq 'SUCCESS') {$counts.Succeeded++ } 
    elseif ($res.Status -eq 'FAILED') {$counts.Failed++ } 
    elseif ($res.Status -eq 'SKIPPED') {$counts.Skipped++ }
    
    $pName =$res.ProjectName.PadRight(20)
    $pStatus =$res.Status.PadRight(10)
    Write-Host "$pName$pStatus $($res.Details)"
}
$sep = [string][char]124
Write-Host "--------------------------------------------------" -ForegroundColor Cyan
Write-Host "Total: $($counts.Succeeded) Succeeded $sep$($counts.Failed) Failed$sep $($counts.Skipped) Skipped" -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan
if ($counts.Failed -gt 0) { exit 1 }
