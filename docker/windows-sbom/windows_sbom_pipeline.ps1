<#
.SYNOPSIS
    Windows container SBOM pipeline.

.DESCRIPTION
    Runs inside the image built from docker/windows-sbom/Dockerfile (NI's
    LabVIEW Windows container plus VIPM, Git, Subversion and Grype). Mirrors
    docker/linux-sbom/linux_sbom_pipeline.py for Windows containers:

      1. Clone the configured project (git or svn).
      2. Deploy the project's VIPM manifest, then run `vipm init`, `vipm lock`
         and `vipm sbom` (CycloneDX 1.5).
      3. Enrich the SBOM: group components, merge vipm.lock packages, and add
         the installed NI dependency closure from `nipkg info-installed`,
         without resolving available feed packages or changing the installation.
      4. Scan the enriched SBOM with Grype and apply the Grype severity gate.
      5. Publish the enriched SBOM to Dependency-Track, export the server-side
         findings, and apply the Critical/High security gate.

    C:\engine, when mounted, is this generic engine's own root
    (Build-Sbom.ps1, pipeline-config.schema.json, vipm-exit-codes.json);
    falls back to C:\workspace for a standalone run with no separate
    engine mount.

    Environment variables (paths are relative to C:\workspace, the mounted repo):
      PIPELINE_CONFIG          Pipeline configuration JSON file (or PROJECTS_OVERRIDE).
      PROJECTS_OVERRIDE        Pipeline configuration JSON text; overrides PIPELINE_CONFIG.
      VIPM_TOML                VIPM manifest file for a single enabled project.
      VIPM_TOML_CONFIGS        JSON object of manifests keyed by project name;
                               overrides VIPM_TOML.
      OUTPUT_DIR               Report directory (default: output/windows).
      ALLOW_PACKAGE_DRIFT      "1" makes `vipm sbom` tolerate installed NIPM package
                               versions that differ from the manifest (default: "0",
                               matching the native pipeline -- strict unless explicitly
                               opted into).
      SBOM_SOURCE              "vipm" (default) runs vipm init/lock/sbom and requires
                               VIPM Pro. "manifest" builds the base SBOM from the
                               deployed vipm.toml without VIPM (no vipm.lock merge).
      SKIP_GRYPE               "1" skips the Grype scan (default: "0").
      GRYPE_FAIL_ON            Lowest Grype severity that fails the run: negligible, low,
                               medium, high (default), or critical. "none" reports only.
      LABVIEW_IMAGE            Base image name recorded in the SBOM metadata.
      LABVIEW_IMAGE_DIGEST     Resolved base image digest recorded in the SBOM metadata.
            DEPENDENCY_TRACK_MODE    "full" (default) or "hosted-grype" reporting policy.
            DEPENDENCY_TRACK_API_KEY Dependency-Track API key required by full mode.
      DEPENDENCY_TRACK_URL     Dependency-Track base URL (required for full mode; no default).
      DEPENDENCY_TRACK_PROJECT_NAME
                               Project name override (default: "<displayName>:<name>-windows-container",
                               kept apart from Run-Pipeline.ps1's "<displayName>:<name>").
      DEPENDENCY_TRACK_SECURITY_GATE
                               Full mode requires the Critical/High gate to remain enabled.
      CI_URL                   Build link recorded in the project description (default: "Local Build").
      SVN_GLOBAL_USER, SVN_GLOBAL_PASSWORD
                               SVN credentials used when an SVN project's configuration
                               supplies no username and password/token.
#>
$ErrorActionPreference = "Stop"

$Workspace = "C:\workspace"
# C:\engine is a second, separate mount of the generic engine's own root
# (see Run-WindowsSbom.ps1 -WorkspaceRoot) -- it holds Build-Sbom.ps1,
# pipeline-config.schema.json and vipm-exit-codes.json even when $Workspace
# is a different, real consumer repository. Falls back to $Workspace for a
# standalone container run with no separate engine mount.
$EngineRoot = if (Test-Path -LiteralPath "C:\engine") { "C:\engine" } else { $Workspace }
. ([System.IO.Path]::Combine($EngineRoot, "Build-Sbom.ps1"))
. (Join-Path $PSScriptRoot "Initialize-VipmContainer.ps1")
$WorkRoot = "C:\sbom-work"
# Groups match the vipm.lock "manager" values.
$NiGroup = "nipm"
$VipmGroup = "vipm"
$SeverityOrder = @("negligible", "low", "medium", "high", "critical")
$NipkgPath = "C:\Program Files\National Instruments\NI Package Manager\nipkg.exe"

class PipelineError : System.Exception {
    PipelineError([string]$Message) : base($Message) {}
}

function Write-SbomLog {
    param([string]$Level, [string]$Message)
    Write-Host "[$((Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))] [$Level] $Message"
}

# Build-Sbom.ps1 helpers log through the native pipeline's function name.
function Write-PipelineLog {
    param([string]$Message, [string]$Level = "INFO")
    Write-SbomLog $Level $Message
}

function Get-EnvValue {
    param([string]$Name, [string]$Default = "")
    $value = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($value)) { return $Default }
    return $value.Trim()
}

function Invoke-Native {
    param(
        [string[]]$Arguments,
        [string]$WorkingDirectory,
        [string]$Redact,
        [switch]$Capture
    )
    $shown = $Arguments -join " "
    if ($Redact) { $shown = $shown.Replace($Redact, "***") }
    Write-SbomLog TRACE "`$ $shown"
    $command = $Arguments[0]
    [string[]]$rest = @($Arguments | Select-Object -Skip 1)
    $output = @()
    if ($WorkingDirectory) { Push-Location -LiteralPath $WorkingDirectory }
    try {
        if ($Capture) {
            $output = @(& $command @rest 2>&1 | ForEach-Object { [string]$_ })
        } else {
            & $command @rest | Out-Host
        }
        $exitCode = $LASTEXITCODE
    } finally {
        if ($WorkingDirectory) { Pop-Location }
    }
    if ($exitCode -ne 0) {
        if ($output.Count -gt 0) {
            $text = $output -join [Environment]::NewLine
            if ($Redact) { $text = $text.Replace($Redact, "***") }
            Write-Host $text
        }
        throw [PipelineError]::new("Command failed with exit code ${exitCode}: $shown")
    }
    if ($Capture) { return ($output -join [Environment]::NewLine) }
}

$script:VipmExitCodes = @{}
$vipmExitCodesPath = [System.IO.Path]::Combine($EngineRoot, "vipm-exit-codes.json")
if (Test-Path -LiteralPath $vipmExitCodesPath -PathType Leaf) {
    $script:VipmExitCodes = ConvertFrom-Json -InputObject ([System.IO.File]::ReadAllText($vipmExitCodesPath)) -AsHashtable
}

function Get-VipmFailureMessage {
    param([string]$Command, [int]$ExitCode, [string[]]$ErrorLines)
    $entry = $script:VipmExitCodes[[string]$ExitCode]
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
    Write-SbomLog TRACE "`$ vipm $($vipmArgs -join ' ')"

    $progress = @{ Step = 1; Last = -1.0; Bucket = -1 }
    $errorState = @{ Lines = [System.Collections.Generic.List[string]]::new(); InError = $false }
    $stdout = [System.Collections.Generic.List[string]]::new()
    $stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
    $vipmPath = (Get-Command vipm -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    $workDir = if ($WorkingDirectory) { $WorkingDirectory } else { (Get-Location).ProviderPath }
    $exitCode = Invoke-StreamingProcess -FilePath $vipmPath -ArgumentList $vipmArgs -WorkingDirectory $workDir -OnLine {
            param([string]$rawLine, [bool]$isStderr)
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
                    Write-SbomLog INFO ("  vipm {0}: step {1} at {2:N0}%" -f $command, $progress.Step, $percent)
                }
            } elseif ($line -match '^\s*error:\s*(.*)$') {
                $errorState.InError = $true
                $errorState.Lines.Add($Matches[1])
                Write-SbomLog ERROR "  vipm $command | $line"
            } elseif ($errorState.InError -and $line -match '^\s+\S') {
                # Indented lines after "error:" itemize it (for example each drifted package).
                $errorState.Lines.Add($line)
                Write-SbomLog ERROR "  vipm $command | $line"
            } elseif ($line -match '^\s*(help|hint|warning|warn):') {
                $errorState.InError = $false
                Write-SbomLog WARN "  vipm $command | $line"
            } elseif ($line -match '^\[VIPM\]\s+(.*)$') {
                Write-SbomLog INFO "  vipm ${command}: $($Matches[1])"
            } else {
                $errorState.InError = $false
                Write-SbomLog INFO "  vipm $command | $line"
            }
    }
    $stopwatch.Stop()
    Write-SbomLog TRACE ("vipm {0} finished with exit code {1} in {2:N1}s." -f $command, $exitCode, $stopwatch.Elapsed.TotalSeconds)
    if ($exitCode -ne 0) {
        throw [PipelineError]::new((Get-VipmFailureMessage -Command $command -ExitCode $exitCode -ErrorLines $errorState.Lines.ToArray()))
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
    Write-SbomLog SUCCESS "Wrote LabVIEW override $LabviewVersion$bitnessText to .vipm/config.toml."
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
    if ([System.IO.Path]::IsPathRooted($VipcPath) -or ($VipcPath -replace '\\', '/').Split('/') -contains "..") {
        throw [PipelineError]::new("vipcPath '$VipcPath' must be a path relative to the checkout root.")
    }
    $resolvedPath = [System.IO.Path]::Combine($ProjectDir, $VipcPath.Replace('/', '\'))
    if (-not (Test-Path -LiteralPath $resolvedPath -PathType Leaf)) {
        throw [PipelineError]::new("vipcPath '$VipcPath' was not found in the checkout.")
    }
    if ([System.IO.Path]::GetExtension($resolvedPath) -ne ".vipc") {
        throw [PipelineError]::new("vipcPath '$VipcPath' is not a .vipc file.")
    }
    $listing = ConvertFrom-Json -InputObject (Invoke-Vipm -Arguments @("list", $resolvedPath) -Json -WorkingDirectory $ProjectDir)
    $packages = @($listing.packages | Where-Object { $_ })
    foreach ($package in $packages) { $expected[[string]$package.name] = [string]$package.version }
    Write-SbomLog INFO "Installing VI Package Configuration '$VipcPath' ($($packages.Count) packages) into LabVIEW $LabviewVersion ($LabviewBitness-bit)..."
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
        Write-SbomLog SUCCESS "Verified all $($expected.Count) .vipc package(s) are installed in LabVIEW $LabviewVersion ($LabviewBitness-bit)."
    } else {
        Write-SbomLog WARN ("vipm install reported success, but $($missing.Count) of $($expected.Count) .vipc package(s) are not installed " +
            "(a package-level install action probably failed; install the .vip on its own to see the error):")
        foreach ($item in $missing) { Write-SbomLog WARN "  not installed: $item" }
    }
    return ,$missing
}

function Get-WorkspacePath {
    param([string]$Relative, [string]$Name)
    if ([System.IO.Path]::IsPathRooted($Relative) -or ($Relative -replace '\\', '/').Split('/') -contains "..") {
        throw [PipelineError]::new("$Name must be a path relative to the repository root: '$Relative'.")
    }
    return [System.IO.Path]::Combine($Workspace, $Relative.Replace('/', '\'))
}

function Copy-GrypeDatabase {
    # Grype cannot activate a downloaded database inside a Windows container volume
    # (directory rename fails), so it runs on a local cache that is synced with the volume.
    param([ValidateSet("Restore", "Save")][string]$Direction)
    $local = Get-EnvValue GRYPE_DB_CACHE_DIR
    $persist = Get-EnvValue GRYPE_DB_PERSIST_DIR
    if (-not $local -or -not $persist -or -not (Test-Path -LiteralPath $persist)) { return }
    # Each container runs as a different ContainerAdministrator account, and files that an
    # earlier container wrote to the volume are not readable until ownership is reclaimed.
    & takeown.exe /f $persist /r /d y 2>&1 | Out-Null
    & icacls.exe $persist /grant "*S-1-1-0:(OI)(CI)F" /t /q 2>&1 | Out-Null
    $source, $target = if ($Direction -eq "Restore") { $persist, $local } else { $local, $persist }
    if (-not (Test-Path -LiteralPath $source) -or -not (Get-ChildItem -LiteralPath $source -Force)) { return }
    $null = New-Item -ItemType Directory -Force -Path $target
    Get-ChildItem -LiteralPath $target -Force | Remove-Item -Recurse -Force
    Copy-Item -Path (Join-Path $source "*") -Destination $target -Recurse -Force
    Write-SbomLog INFO "$(if ($Direction -eq 'Restore') { 'Restored' } else { 'Saved' }) the Grype database cache ($persist)."
}

function Resolve-ConfigValue {
    param($Value)
    if ($null -eq $Value) { return $null }
    $text = ([string]$Value).Trim()
    if ($text -match '^\$env:(.+)$') { return [Environment]::GetEnvironmentVariable($Matches[1]) }
    return $text
}

function Get-RelativeOutputPath {
    param([string]$Path)
    return [System.IO.Path]::GetRelativePath($Workspace, $Path).Replace('\', '/')
}

# ---------------------------------------------------------------- TOML helpers
function Get-TomlLineContent {
    param([string]$Line)
    $quote = [char]0
    $escaped = $false
    for ($index = 0; $index -lt $Line.Length; $index++) {
        $character = $Line[$index]
        if ($quote -ne [char]0) {
            if ($quote -eq '"' -and $character -eq '\' -and -not $escaped) {
                $escaped = $true
                continue
            }
            if ($character -eq $quote -and -not $escaped) { $quote = [char]0 }
            $escaped = $false
            continue
        }
        if ($character -eq '"' -or $character -eq "'") {
            $quote = $character
        } elseif ($character -eq '#') {
            return $Line.Substring(0, $index)
        }
    }
    return $Line
}

function ConvertFrom-TomlScalar {
    param([string]$Value)
    $text = $Value.Trim()
    if ($text.StartsWith('"')) { return ConvertFrom-Json -InputObject $text }
    if ($text.Length -ge 2 -and $text.StartsWith("'") -and $text.EndsWith("'")) {
        return $text.Substring(1, $text.Length - 2)
    }
    return $text
}

function Read-TomlTable {
    # Returns @{ table-name = [ordered]@{ key = value } } for simple key/value TOML tables.
    param([string]$Path)
    $tables = @{ "" = [ordered]@{} }
    $section = ""
    foreach ($raw in [System.IO.File]::ReadAllLines($Path)) {
        $line = (Get-TomlLineContent -Line $raw).Trim()
        if ($line -match '^\[([^\[\]]+)\]$') {
            $section = $Matches[1].Trim()
            if (-not $tables.ContainsKey($section)) { $tables[$section] = [ordered]@{} }
            continue
        }
        if (-not $line.StartsWith("[[") -and $line -match '^("[^"]+"|[A-Za-z0-9_.-]+)\s*=\s*(.*)$') {
            if (-not $tables.ContainsKey($section)) { $tables[$section] = [ordered]@{} }
            $tables[$section][$Matches[1].Trim('"')] = ConvertFrom-TomlScalar -Value $Matches[2]
        }
    }
    return $tables
}

function Read-LockPackage {
    param([string]$Path)
    $packages = [System.Collections.Generic.List[hashtable]]::new()
    $current = $null
    foreach ($raw in [System.IO.File]::ReadAllLines($Path)) {
        $line = (Get-TomlLineContent -Line $raw).Trim()
        if ($line.StartsWith("[")) {
            $current = $null
            if ($line -eq "[[package]]") {
                $current = @{}
                $packages.Add($current)
            }
            continue
        }
        if ($null -ne $current -and $line -match '^(name|version|manager)\s*=\s*(.*)$') {
            $current[$Matches[1]] = [string](ConvertFrom-TomlScalar -Value $Matches[2])
        }
    }
    return , $packages.ToArray()
}

# ---------------------------------------------------------------- nipkg scan
function ConvertFrom-NipkgListing {
    # nipkg list output: "<name>\t<version>\t<architecture>\t<description>" per package.
    param([string]$Text)
    $packages = [ordered]@{}
    foreach ($line in ($Text -split "`r?`n")) {
        $fields = $line.Split("`t")
        if ($fields.Count -ge 3 -and $fields[2] -match '^windows_') {
            $packages[$fields[0].ToLowerInvariant()] = @{
                name        = $fields[0]
                version     = $fields[1]
                arch        = $fields[2]
                description = if ($fields.Count -ge 4) { ($fields[3..($fields.Count - 1)] -join "`t").Trim() } else { "" }
            }
        }
    }
    return $packages
}

function ConvertFrom-NipkgInstalledInfo {
    param([string]$Text)
    # Container NIPM doubles field/record separators, but not embedded description newlines.
    if ($Text -notmatch '(?m)^[A-Za-z][A-Za-z0-9-]*:[^\r\n]*\r?\n[A-Za-z][A-Za-z0-9-]*:') {
        $Text = $Text -replace '\r?\n\r?\n', "`n"
    }
    $packages = [ordered]@{}
    foreach ($record in ($Text.Trim() -split '(?:\r?\n)[ \t]*(?:\r?\n)')) {
        if ([string]::IsNullOrWhiteSpace($record)) { continue }
        $fields = @{}
        $field = $null
        foreach ($line in ($record -split '\r?\n')) {
            if ($line -match '^\s+' -and $field) {
                $fields[$field] += " " + $line.Trim()
            } elseif ($line -match '^([A-Za-z][A-Za-z0-9-]*):\s*(.*)$') {
                $field = $Matches[1]
                if ($fields.ContainsKey($field)) {
                    if ($fields[$field] -ceq $Matches[2]) { continue }
                    throw [PipelineError]::new("Duplicate NI metadata field '$field' has conflicting values.")
                }
                $fields[$field] = $Matches[2]
            } else {
                throw [PipelineError]::new("Invalid installed NI metadata line: '$line'.")
            }
        }
        foreach ($required in "Package", "Version", "Architecture") {
            if ([string]::IsNullOrWhiteSpace($fields[$required])) {
                throw [PipelineError]::new("Installed NI metadata is missing '$required'.")
            }
        }
        $key = $fields.Package.ToLowerInvariant()
        if ($packages.Contains($key)) { throw [PipelineError]::new("Duplicate installed NI package '$key'.") }
        $packages[$key] = @{
            name = $fields.Package; version = $fields.Version; arch = $fields.Architecture
            description = [string]$fields.Description; depends = [string]$fields.Depends
            provides = [string]$fields.Provides
        }
    }
    if ($packages.Count -eq 0) { throw [PipelineError]::new("nipkg info-installed returned no package metadata.") }
    return $packages
}

function Get-NipkgRelationshipName {
    param([string]$Text)
    if ([string]::IsNullOrWhiteSpace($Text)) { return }
    foreach ($entry in ($Text -split ',')) {
        if ($entry.Trim() -notmatch '^([A-Za-z0-9][A-Za-z0-9_.+-]*)(?:\s*\(\s*(?:>=|<=|=|>>|<<|>|<)\s*[^()\s]+\s*\))?$') {
            throw [PipelineError]::new("Unsupported installed NI dependency/provides expression '$entry'.")
        }
        $Matches[1].ToLowerInvariant()
    }
}

function Get-InstalledNipkgClosure {
    param([string[]]$Roots)
    $installed = ConvertFrom-NipkgListing -Text (Invoke-Native -Arguments @($NipkgPath, "list-installed") -Capture)
    $metadata = ConvertFrom-NipkgInstalledInfo -Text (Invoke-Native -Arguments @($NipkgPath, "info-installed") -Capture)
    if ($installed.Count -eq 0 -or $installed.Count -ne $metadata.Count) {
        throw [PipelineError]::new("Installed NI listing and metadata have different package counts.")
    }
    $providers = @{}
    foreach ($key in $metadata.Keys) {
        $package = $metadata[$key]
        if (-not $installed.Contains($key) -or $installed[$key].version -ne $package.version -or $installed[$key].arch -ne $package.arch) {
            throw [PipelineError]::new("Installed NI listing and metadata disagree for '$key'.")
        }
        foreach ($alias in @(Get-NipkgRelationshipName -Text $package.provides)) {
            if (-not $providers.ContainsKey($alias)) { $providers[$alias] = [System.Collections.Generic.List[string]]::new() }
            if (-not $providers[$alias].Contains($key)) { $providers[$alias].Add($key) }
        }
    }
    $resolve = {
        param([string]$Name)
        $key = $Name.ToLowerInvariant()
        if ($metadata.Contains($key)) { return $key }
        if (-not $providers.ContainsKey($key)) {
            throw [PipelineError]::new("Required NI package '$Name' has no installed package or provider metadata.")
        }
        if ($providers[$key].Count -ne 1) {
            throw [PipelineError]::new("Required NI package '$Name' has ambiguous installed providers: $($providers[$key] -join ', ').")
        }
        return $providers[$key][0]
    }
    $graph = [ordered]@{}
    $packages = [ordered]@{}
    $visit = {
        param([string]$Key)
        if ($graph.Contains($Key)) { return }
        $graph[$Key] = [System.Collections.Generic.List[string]]::new()
        $packages[$Key] = $metadata[$Key]
        foreach ($name in @(Get-NipkgRelationshipName -Text $metadata[$Key].depends)) {
            $child = & $resolve $name
            if ($child -eq $Key) { continue }
            if (-not $graph[$Key].Contains($child)) { $graph[$Key].Add($child) }
            & $visit $child
        }
    }
    $resolvedRoots = @{}
    foreach ($name in $Roots) { $resolvedRoots[$name.ToLowerInvariant()] = & $resolve $name }
    $mappedRoots = @($resolvedRoots.Values | Select-Object -Unique)
    foreach ($key in $mappedRoots) { & $visit $key }
    return @{ Packages = $packages; Graph = $graph; Roots = $mappedRoots; ResolvedRoots = $resolvedRoots }
}

function ConvertTo-NipkgComponent {
    param([hashtable]$Package)
    $purl = "pkg:nipkg/$([uri]::EscapeDataString($Package.name))@$([uri]::EscapeDataString($Package.version))"
    return [ordered]@{
        "bom-ref"   = $purl
        type        = "library"
        group       = $NiGroup
        name        = $Package.name
        version     = $Package.version
        description = $Package.description
        purl        = $purl
        supplier    = [ordered]@{ name = "National Instruments" }
        properties  = @(
            [ordered]@{ name = "nipm:inventory-source"; value = "nipkg info-installed" }
            [ordered]@{ name = "nipm:installed-depends"; value = $Package.depends }
            [ordered]@{ name = "nipm:installed-provides"; value = $Package.provides }
        )
    }
}

# ---------------------------------------------------------------- enrichment
function Add-SbomProperty {
    param($Sbom, [System.Collections.IDictionary]$Properties)
    $metadata = $Sbom["metadata"]
    $list = [System.Collections.Generic.List[object]]::new()
    if ($metadata.Contains("properties") -and $metadata["properties"]) { $list.AddRange([object[]]@($metadata["properties"])) }
    foreach ($name in $Properties.Keys) { $list.Add([ordered]@{ name = $name; value = [string]$Properties[$name] }) }
    $metadata["properties"] = $list.ToArray()
}

function Get-SbomPropertyValue {
    param($Sbom, [string]$Name, [string]$Default = "unknown")
    foreach ($property in @($Sbom["metadata"]["properties"])) {
        if ($property -and $property["name"] -eq $Name -and $property["value"]) { return [string]$property["value"] }
    }
    return $Default
}

function ConvertTo-LockComponent {
    param($Package)
    $purl = "pkg:generic/$([uri]::EscapeDataString($Package.name))@$([uri]::EscapeDataString($Package.version))"
    return [ordered]@{ "bom-ref" = $purl; type = "library"; group = $Package.manager
                       name = $Package.name; version = $Package.version; purl = $purl }
}

function Add-LockedVersionProperty {
    # Records the vipm.lock version on the installed NIPM component it resolves to.
    param([System.Collections.IDictionary]$Component, $Package)
    $component["group"] = $Package.manager
    $list = [System.Collections.Generic.List[object]]::new()
    if ($Component.Contains("properties") -and $Component["properties"]) {
        $list.AddRange([object[]]@($Component["properties"] | Where-Object { $_["name"] -notin "vipm:locked-version", "vipm:version-drift" }))
    }
    $list.Add([ordered]@{ name = "vipm:locked-version"; value = [string]$Package.version })
    if ([string]$Component["version"] -ne [string]$Package.version) {
        $list.Add([ordered]@{ name = "vipm:version-drift"; value = "true" })
    }
    $Component["properties"] = $list.ToArray()
}

function Add-SbomEnrichment {
    param($Sbom, [hashtable]$Manifest, [object[]]$LockPackages)

    $vipmDeps = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    if ($Manifest.ContainsKey("dependencies")) { foreach ($key in $Manifest["dependencies"].Keys) { [void]$vipmDeps.Add($key) } }
    $nipmRoots = if ($Manifest.ContainsKey("nipm.dependencies")) { $Manifest["nipm.dependencies"] } else { [ordered]@{} }
    $nipmRootNames = [System.Collections.Generic.HashSet[string]]::new([string[]]@($nipmRoots.Keys), [StringComparer]::OrdinalIgnoreCase)

    $components = [System.Collections.Generic.List[object]]::new()
    if ($Sbom.Contains("components") -and $Sbom["components"]) { $components.AddRange([object[]]@($Sbom["components"])) }
    $nameToRef = @{}
    foreach ($component in $components) {
        $name = ([string]$component["name"]).ToLowerInvariant()
        if ($vipmDeps.Contains($name)) {
            $component["group"] = $VipmGroup
        } elseif ($nipmRootNames.Contains($name) -or ([string]$component["purl"]).StartsWith("pkg:nipkg/")) {
            $component["group"] = $NiGroup
        }
        $nameToRef[$name] = [string]$component["bom-ref"]
    }

    $pendingNipm = [System.Collections.Generic.List[object]]::new()
    foreach ($package in $LockPackages) {
        foreach ($key in "name", "version", "manager") {
            if ([string]::IsNullOrWhiteSpace($package[$key])) {
                throw [PipelineError]::new("Generated vipm.lock contains a package missing name, version, or manager.")
            }
        }
        if ($package.manager -eq "nipm") {
            $pendingNipm.Add($package)
            continue
        }
        $component = $components | Where-Object { $_["name"] -eq $package.name -and $_["version"] -eq $package.version } | Select-Object -First 1
        if ($null -eq $component) {
            $component = ConvertTo-LockComponent -Package $package
            $components.Add($component)
        } else {
            $component["type"] = "library"
            $component["group"] = $package.manager
        }
        $nameToRef[$package.name.ToLowerInvariant()] = [string]$component["bom-ref"]
    }

    $depNodes = [ordered]@{}
    if ($Sbom.Contains("dependencies") -and $Sbom["dependencies"]) {
        foreach ($node in @($Sbom["dependencies"])) {
            $list = [System.Collections.Generic.List[string]]::new()
            foreach ($dep in @($node["dependsOn"])) { if ($dep) { $list.Add([string]$dep) } }
            $depNodes[[string]$node["ref"]] = $list
        }
    }
    $rootRef = [string]$Sbom["metadata"]["component"]["bom-ref"]
    if (-not $depNodes.Contains($rootRef)) { $depNodes[$rootRef] = [System.Collections.Generic.List[string]]::new() }
    $addEdge = {
        param([string]$Parent, [string]$Child)
        if (-not $Parent -or -not $Child -or $Parent -eq $Child) { return }
        if (-not $depNodes.Contains($Parent)) { $depNodes[$Parent] = [System.Collections.Generic.List[string]]::new() }
        if (-not $depNodes[$Parent].Contains($Child)) { $depNodes[$Parent].Add($Child) }
    }

    $added = 0
    $existingNipmNames = @($components | Where-Object { ([string]$_["purl"]).StartsWith("pkg:nipkg/") } | ForEach-Object { $_["name"] })
    if ($nipmRoots.Count -eq 0 -and $pendingNipm.Count -eq 0 -and $existingNipmNames.Count -eq 0) {
        Write-SbomLog WARN "No [nipm.dependencies] in the manifest. Skipping the nipkg deep scan."
    } else {
        if (-not (Test-Path -LiteralPath $NipkgPath -PathType Leaf)) {
            throw [PipelineError]::new("nipkg.exe was not found at '$NipkgPath'.")
        }
        Write-SbomLog INFO "Building the installed NI dependency graph with nipkg info-installed (no feed resolution)..."
        $roots = @($nipmRoots.Keys) + @($pendingNipm | ForEach-Object name) + $existingNipmNames
        $closure = Get-InstalledNipkgClosure -Roots $roots
        $scanned = $closure.Packages
        $graph = $closure.Graph
        $directRoots = if ($nipmRoots.Count -gt 0) { @($nipmRoots.Keys) }
        elseif ($pendingNipm.Count -gt 0) { @($pendingNipm | ForEach-Object name) }
        else { $existingNipmNames }
        $mappedRoots = @($directRoots | ForEach-Object { $closure.ResolvedRoots[$_.ToLowerInvariant()] } | Select-Object -Unique)

        $remap = @{}
        foreach ($key in @($scanned.Keys)) {
            $component = ConvertTo-NipkgComponent -Package $scanned[$key]
            $old = @($components | Where-Object {
                $_["name"] -eq $scanned[$key].name -or $closure.ResolvedRoots[[string]$_["name"]] -eq $key
            })
            foreach ($previous in $old) {
                if ($previous["name"] -eq $component["name"] -and $previous["version"] -eq $component["version"]) {
                    foreach ($field in $previous.Keys) {
                        if (-not $component.Contains($field)) { $component[$field] = $previous[$field] }
                    }
                    $component["properties"] += @($previous["properties"] | Where-Object {
                        $_ -and $_["name"] -notin "nipm:inventory-source", "nipm:installed-depends", "nipm:installed-provides"
                    })
                }
                $remap[[string]$previous["bom-ref"]] = [string]$component["bom-ref"]
                [void]$components.Remove($previous)
            }
            if ($old.Count -eq 0) { $added++ }
            $components.Add($component)
            $nameToRef[$key] = [string]$component["bom-ref"]
        }
        foreach ($alias in $closure.ResolvedRoots.Keys) { $nameToRef[$alias] = $nameToRef[$closure.ResolvedRoots[$alias]] }
        # Discard feed-selected outgoing edges for installed packages; retain/remap other graph edges.
        $previousNodes = $depNodes
        $depNodes = [ordered]@{}
        foreach ($parent in $previousNodes.Keys) {
            if ($remap.ContainsKey($parent)) { continue }
            if (-not $depNodes.Contains($parent)) { $depNodes[$parent] = [System.Collections.Generic.List[string]]::new() }
            foreach ($child in $previousNodes[$parent]) {
                $target = if ($remap.ContainsKey($child)) { $remap[$child] } else { $child }
                & $addEdge $parent $target
            }
        }
        foreach ($key in $mappedRoots) { & $addEdge $rootRef $nameToRef[$key] }
        foreach ($parent in @($graph.Keys)) {
            foreach ($child in $graph[$parent]) { & $addEdge $nameToRef[$parent] $nameToRef[$child] }
        }
        Write-SbomLog SUCCESS "Resolved $($scanned.Count) NI packages; added $added new components and their dependency relationships to the graph."
        Add-SbomProperty -Sbom $Sbom -Properties @{ "nipm:inventory-source" = "nipkg info-installed" }
    }

    foreach ($package in $pendingNipm) {
        $key = $package.name.ToLowerInvariant()
        $component = if ($nameToRef.ContainsKey($key)) { $components | Where-Object { $_["bom-ref"] -eq $nameToRef[$key] } | Select-Object -First 1 }
        if ($null -eq $component) {
            throw [PipelineError]::new("Locked NI package '$($package.name)' could not be mapped to installed inventory.")
        } else {
            Add-LockedVersionProperty -Component $component -Package $package
        }
    }

    $Sbom["components"] = $components.ToArray()
    $dependencies = [System.Collections.Generic.List[object]]::new()
    foreach ($ref in @($depNodes.Keys)) {
        $dependencies.Add([ordered]@{ ref = $ref; dependsOn = $depNodes[$ref].ToArray() })
    }
    $Sbom["dependencies"] = $dependencies.ToArray()

    $os = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion"
    Add-SbomProperty -Sbom $Sbom -Properties ([ordered]@{
        "sbom:platform"                  = "windows"
        "sbom:os"                        = "$($os.ProductName) $($os.DisplayVersion) (build $($os.CurrentBuildNumber).$($os.UBR))"
        "labview:container-image"        = Get-EnvValue LABVIEW_IMAGE "unknown"
        "labview:container-image-digest" = Get-EnvValue LABVIEW_IMAGE_DIGEST "unknown"
    })
    return $added
}

# ---------------------------------------------------------------- pipeline
function Get-PipelineConfig {
    $overrideText = [Environment]::GetEnvironmentVariable("PROJECTS_OVERRIDE")
    if (-not [string]::IsNullOrWhiteSpace($overrideText)) {
        Write-SbomLog INFO "Using PROJECTS_OVERRIDE configuration."
        $text = $overrideText
    } else {
        $relative = Get-EnvValue PIPELINE_CONFIG
        if (-not $relative) { throw [PipelineError]::new("Set PIPELINE_CONFIG or PROJECTS_OVERRIDE.") }
        Write-SbomLog INFO "Using configuration '$relative'."
        $text = [System.IO.File]::ReadAllText((Get-WorkspacePath -Relative $relative -Name PIPELINE_CONFIG))
    }
    $config = ConvertFrom-Json -InputObject $text -AsHashtable
    if ($config -isnot [System.Collections.IDictionary] -or -not $config["projects"]) {
        throw [PipelineError]::new("Configuration must be an object with a non-empty 'projects' array.")
    }
    $schema = [System.IO.File]::ReadAllText([System.IO.Path]::Combine($EngineRoot, "pipeline-config.schema.json"))
    if (-not (Test-Json -Json $text -Schema $schema)) {
        throw [PipelineError]::new("Pipeline configuration does not match pipeline-config.schema.json.")
    }
    return $config
}

function Get-VipmManifest {
    param([string[]]$EnabledProjects)
    $manifestsJson = [Environment]::GetEnvironmentVariable("VIPM_TOML_CONFIGS")
    if (-not [string]::IsNullOrWhiteSpace($manifestsJson)) {
        $manifests = ConvertFrom-Json -InputObject $manifestsJson -AsHashtable
        if ($manifests -isnot [System.Collections.IDictionary]) {
            throw [PipelineError]::new("VIPM_TOML_CONFIGS must be a JSON object keyed by project name.")
        }
        return $manifests
    }
    $relative = Get-EnvValue VIPM_TOML
    if (-not $relative) { throw [PipelineError]::new("Set VIPM_TOML or VIPM_TOML_CONFIGS.") }
    if ($EnabledProjects.Count -ne 1) {
        throw [PipelineError]::new("VIPM_TOML supplies one manifest; use VIPM_TOML_CONFIGS for multiple enabled projects.")
    }
    return @{ $EnabledProjects[0] = [System.IO.File]::ReadAllText((Get-WorkspacePath -Relative $relative -Name VIPM_TOML)) }
}

function Invoke-Checkout {
    param($Project, $Config, [string]$Destination)
    $vcs = ([string]$Project["vcs"]).Trim().ToLowerInvariant()
    if ($vcs -notin "git", "svn") { throw [PipelineError]::new("Project must define 'vcs' as 'git' or 'svn'.") }
    $baseUrls = if ($Config["baseUrls"]) { $Config["baseUrls"] } else { @{} }
    if ($Project["repoUrl"]) {
        $repoUrl = ([string]$Project["repoUrl"]).Trim()
    } elseif ($baseUrls[$vcs]) {
        $repoUrl = ([string]$baseUrls[$vcs]).Trim().TrimEnd('/') + "/" + ([string]$Project["name"]).Trim()
    } else {
        throw [PipelineError]::new("No 'repoUrl' and no baseUrls.$vcs for project.")
    }
    $revision = if ($Project["revision"]) { ([string]$Project["revision"]).Trim() } else { "HEAD" }
    $username = Resolve-ConfigValue $(if ($Project["username"]) { $Project["username"] } else { $Config["username"] })
    $token = Resolve-ConfigValue $(if ($Project["token"]) { $Project["token"] } else { $Config["token"] })
    $password = Resolve-ConfigValue $(if ($Project["password"]) { $Project["password"] } else { $Config["password"] })
    $secret = if ($token) { $token } else { $password }
    if ($vcs -eq "svn" -and -not ($username -and $secret)) {
        # Configured credentials win; otherwise use the global SVN credentials passed to the container.
        $globalUser = Get-EnvValue SVN_GLOBAL_USER
        $globalPassword = Get-EnvValue SVN_GLOBAL_PASSWORD
        if ($globalUser -and $globalPassword) {
            Write-SbomLog INFO "Using SVN_GLOBAL_USER/SVN_GLOBAL_PASSWORD for the SVN checkout."
            $username = $globalUser
            $secret = $globalPassword
        } elseif ($globalUser -or $globalPassword) {
            Write-SbomLog WARN "Only one of SVN_GLOBAL_USER and SVN_GLOBAL_PASSWORD is set; checking out without credentials."
        }
    }

    if ($vcs -eq "git") {
        $cloneUrl = $repoUrl
        if ($secret -and $repoUrl.StartsWith("https://")) { $cloneUrl = "https://$secret@" + $repoUrl.Substring(8) }
        Write-SbomLog INFO "Cloning $repoUrl..."
        Invoke-Native -Arguments @("git", "clone", "--quiet", $cloneUrl, $Destination) -Redact $secret
        if ($revision -notin "", "HEAD") {
            Write-SbomLog INFO "Checking out git revision $revision..."
            Invoke-Native -Arguments @("git", "-C", $Destination, "checkout", "--quiet", $revision)
        }
        $resolved = (Invoke-Native -Arguments @("git", "-C", $Destination, "rev-parse", "HEAD") -Capture).Trim()
    } else {
        Write-SbomLog INFO "Checking out $repoUrl at revision $revision..."
        $sparsePaths = @($Project["sparsePaths"] | Where-Object { $_ } | ForEach-Object { ([string]$_).Trim() })
        $svnAuth = @("--non-interactive", "--trust-server-cert", "--quiet")
        if ($username -and $secret) { $svnAuth += @("--username", $username, "--password", $secret) }
        $svnArgs = @("svn", "checkout", $repoUrl, $Destination, "--revision", $revision) + $svnAuth
        if ($sparsePaths.Count -gt 0) { $svnArgs += @("--depth", "empty") }
        Invoke-Native -Arguments $svnArgs -Redact $secret
        $resolved = (Invoke-Native -Arguments @("svn", "info", "--show-item", "revision", $Destination) -Capture).Trim()
        foreach ($step in (Get-SvnSparseUpdateStep -Paths $sparsePaths)) {
            Write-SbomLog INFO "Sparse checkout: $($step.Path) (depth $($step.Depth)) at r$resolved"
            Invoke-Native -Arguments (@("svn", "update", "--set-depth", $step.Depth, "--revision", $resolved,
                    [System.IO.Path]::Combine($Destination, $step.Path)) + $svnAuth) -Redact $secret
        }
    }
    Write-SbomLog INFO "Checked out $vcs revision $resolved."
    return @{ RepoUrl = $repoUrl; Vcs = $vcs; Revision = $resolved }
}

function ConvertTo-ManifestSbom {
    # Builds a CycloneDX 1.5 SBOM from vipm.toml when VIPM Pro is unavailable.
    param([hashtable]$Manifest, [string]$Name, [string]$Version, [string]$LvVersion, [string]$LvBitness)
    $purl = { param($Kind, $Package, $PackageVersion)
        "pkg:$Kind/$([uri]::EscapeDataString($Package))@$([uri]::EscapeDataString($PackageVersion))" }
    $rootRef = & $purl "generic" $Name $Version
    $components = [System.Collections.Generic.List[object]]::new()
    foreach ($section in @(@("dependencies", "generic"), @("nipm.dependencies", "nipkg"))) {
        if (-not $Manifest.ContainsKey($section[0])) { continue }
        foreach ($package in @($Manifest[$section[0]].Keys)) {
            $packageVersion = (([string]$Manifest[$section[0]][$package]) -replace '^[\s^~=<>!]+', '').Split(",")[0].Trim()
            if (-not $packageVersion) { $packageVersion = "unspecified" }
            $ref = & $purl $section[1] $package $packageVersion
            $components.Add([ordered]@{ "bom-ref" = $ref; type = "library"; name = $package; version = $packageVersion; purl = $ref })
        }
    }
    $dependsOn = [string[]]@($components | ForEach-Object { $_["bom-ref"] })
    return [ordered]@{
        bomFormat    = "CycloneDX"
        specVersion  = "1.5"
        serialNumber = "urn:uuid:$([guid]::NewGuid())"
        version      = 1
        metadata     = [ordered]@{
            timestamp  = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")
            tools      = [ordered]@{ components = @([ordered]@{ type = "application"; name = "windows-sbom-pipeline-manifest-mode"; version = "1.0.0" }) }
            component  = [ordered]@{ "bom-ref" = $rootRef; type = "application"; name = $Name; version = $Version }
            properties = @(
                [ordered]@{ name = "sbom:source"; value = "vipm.toml (manifest mode, no vipm sbom scan)" },
                [ordered]@{ name = "labview:version"; value = $LvVersion },
                [ordered]@{ name = "labview:bitness"; value = $LvBitness }
            )
        }
        components   = $components.ToArray()
        dependencies = @([ordered]@{ ref = $rootRef; dependsOn = $dependsOn })
    }
}

function Invoke-DependencyTrack {
    param([string]$BaseUrl, [string]$ApiKey, [string]$Method, [string]$Path, $Payload, [switch]$Raw)
    Write-SbomLog TRACE "Dependency-Track $Method $BaseUrl$Path"
    $request = @{
        Method     = $Method
        Uri        = "$BaseUrl$Path"
        Headers    = @{ "X-Api-Key" = $ApiKey; Accept = "application/json" }
        TimeoutSec = 120
    }
    if ($null -ne $Payload) {
        $request.Body = [System.Text.Encoding]::UTF8.GetBytes((ConvertTo-Json -InputObject $Payload -Depth 10 -Compress))
        $request.ContentType = "application/json"
    }
    try {
        if ($Raw) { return (Invoke-WebRequest @request).Content }
        return Invoke-RestMethod @request
    } catch {
        $status = $_.Exception.Response.StatusCode
        $detail = if ($status) { "HTTP $([int]$status) $status" } else { $_.Exception.Message }
        throw [PipelineError]::new("Dependency-Track $Method $Path failed: $detail")
    }
}

function Publish-DependencyTrack {
    param($Sbom, [string]$SbomPath, [string]$ProjectName, [string]$ProjectVersion, [string]$ExportPath,
          [string]$LvVersion, [string]$LvBitness, [System.Collections.IDictionary]$Record)
    $baseUrl = (Get-EnvValue DEPENDENCY_TRACK_URL).TrimEnd('/')
    $apiKey = Get-EnvValue DEPENDENCY_TRACK_API_KEY
    $dt = {
        param($Method, $Path, $Payload, [switch]$Raw)
        Invoke-DependencyTrack -BaseUrl $baseUrl -ApiKey $apiKey -Method $Method -Path $Path -Payload $Payload -Raw:$Raw
    }
    $query = "name=$([uri]::EscapeDataString($ProjectName))&version=$([uri]::EscapeDataString($ProjectVersion))"
    $previousImport = 0L
    try {
        $previousProject = & $dt GET "/api/v1/project/lookup?$query" $null
        if ($previousProject.lastBomImport) { $previousImport = [long]$previousProject.lastBomImport }
    } catch [PipelineError] {
        if ($_.Exception.Message -notmatch 'HTTP 404') {
            throw [PipelineError]::new("Dependency-Track project baseline lookup failed: $($_.Exception.Message)")
        }
    }
    Write-SbomLog INFO "Pushing enriched SBOM to Dependency-Track ($baseUrl) as '$ProjectName' version '$ProjectVersion'..."

    $upload = & $dt PUT "/api/v1/bom" @{
        projectName    = $ProjectName
        projectVersion = $ProjectVersion
        autoCreate     = $true
        bom            = [Convert]::ToBase64String([System.IO.File]::ReadAllBytes($SbomPath))
    }
    if (-not $upload.token) { throw [PipelineError]::new("Dependency-Track did not return a BOM processing token.") }
    $deadline = [DateTime]::UtcNow.AddSeconds(180)
    while ((& $dt GET "/api/v1/bom/token/$($upload.token)" $null).processing) {
        if ([DateTime]::UtcNow -gt $deadline) { throw [PipelineError]::new("Timed out waiting for Dependency-Track to process the SBOM.") }
        Start-Sleep -Seconds 2
    }
    Write-SbomLog SUCCESS "Dependency-Track finished processing the SBOM."

    $project = & $dt GET "/api/v1/project/lookup?$query" $null
    if (-not $project.uuid) { throw [PipelineError]::new("Could not resolve the Dependency-Track project UUID.") }
    $bomImport = if ($project.lastBomImport) { [long]$project.lastBomImport } else { 0L }
    if ($bomImport -le $previousImport) { throw [PipelineError]::new("Dependency-Track did not record a fresh BOM import for $ProjectName.") }

    $image = Get-SbomPropertyValue -Sbom $Sbom -Name "labview:container-image-digest"
    if ($image -eq "unknown") { $image = Get-SbomPropertyValue -Sbom $Sbom -Name "labview:container-image" }
    $description = [System.Collections.Generic.List[string]]::new()
    $description.Add("Revision: $(Get-SbomPropertyValue -Sbom $Sbom -Name 'vcs:revision')")
    $description.Add("CI/CD Link: $(Get-EnvValue CI_URL 'Local Build')")
    $description.Add("Image: $image")
    $tags = [System.Collections.Generic.SortedSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($tag in "platform-windows-container", "vcs-$(Get-SbomPropertyValue -Sbom $Sbom -Name 'vcs:type')",
                     "labview-$LvVersion", "bitness-$LvBitness") { [void]$tags.Add($tag) }
    $tools = $Sbom["metadata"]["tools"]
    $toolList = if ($tools -is [System.Collections.IDictionary]) { @($tools["components"]) } else { @($tools) }
    foreach ($tool in $toolList) {
        if (-not $tool) { continue }
        $toolName = ([string]$tool["name"]) -replace '[^a-zA-Z0-9_-]', ''
        if ($toolName -and $tool["version"]) {
            $description.Add("${toolName}: $($tool['version'])")
            [void]$tags.Add("$($toolName.ToLowerInvariant())-$($tool['version'])")
        }
    }
    try {
        [void](& $dt PATCH "/api/v1/project/$($project.uuid)" @{
            description = $description -join " | "
            tags        = @($tags | ForEach-Object { @{ name = $_ } })
        })
    } catch [PipelineError] {
        Write-SbomLog WARN "Non-fatal error patching project metadata: $($_.Exception.Message)"
    }

    try {
        $findings = & $dt GET "/api/v1/finding/project/$($project.uuid)/export" $null -Raw
        if ($findings -is [byte[]]) { [System.IO.File]::WriteAllBytes($ExportPath, $findings) }
        else { [System.IO.File]::WriteAllText($ExportPath, [string]$findings, [System.Text.UTF8Encoding]::new($false)) }
        Write-SbomLog SUCCESS "Server-side findings exported to $(Get-RelativeOutputPath $ExportPath)."
    } catch [PipelineError] {
        if ($_.Exception.Message -match "HTTP 403") {
            Write-SbomLog ERROR "403 Forbidden exporting findings. Ensure the API key has the VIEW_VULNERABILITY permission and team access to the project."
        } else {
            Write-SbomLog WARN "Failed to export findings: $($_.Exception.Message)"
        }
        Set-PipelineFindingsExportFailure -Record $Record -Message $_.Exception.Message
    }

    # New uploads have no metrics until Dependency-Track recalculates them.
    try {
        [void](& $dt GET "/api/v1/metrics/project/$($project.uuid)/refresh" $null)
    } catch [PipelineError] {
        Write-SbomLog WARN "Could not request a metrics refresh: $($_.Exception.Message)"
    }
    $metrics = $null
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        $metrics = & $dt GET "/api/v1/metrics/project/$($project.uuid)/current" $null
        if ($metrics -and [long]$metrics.lastOccurrence -ge $bomImport) { break }
        if ($attempt -lt 19) { Start-Sleep -Seconds 3 }
    }
    if (-not $metrics) { throw [PipelineError]::new("Dependency-Track returned no project metrics; the security gate was not evaluated.") }
    if ([long]$metrics.lastOccurrence -lt $bomImport) { throw [PipelineError]::new("Dependency-Track metrics did not refresh for the uploaded BOM in $ProjectName.") }
    if ($null -eq $metrics.critical -or $null -eq $metrics.high) { throw [PipelineError]::new("Dependency-Track metrics are missing Critical or High counts for $ProjectName.") }

    $c = [int]$metrics.critical; $h = [int]$metrics.high; $m = [int]$metrics.medium; $l = [int]$metrics.low
    Write-SbomLog METRIC "Risk Metrics Gate -> Critical: $c | High: $h | Medium: $m | Low: $l"
    if ($c -or $h) {
        $message = "SECURITY GATE FAILED: Found $c Critical and $h High vulnerabilities in $ProjectName."
        Set-PipelineStage -Record $Record -Name "dependencyTrack" -Status completed -Gate failed
        throw [PipelineError]::new($message)
    } else {
        Write-SbomLog SUCCESS "Security Gate Passed!"
        Set-PipelineStage -Record $Record -Name "dependencyTrack" -Status completed -Gate passed
    }
    return "Dependency-Track C/H/M/L $c/$h/$m/$l"
}

function Test-GrypeGate {
    # Returns an error message when findings reach GRYPE_FAIL_ON, else $null.
    param([object[]]$Findings, [string]$ProjectName)
    $threshold = (Get-EnvValue GRYPE_FAIL_ON "high").ToLowerInvariant()
    if ($threshold -eq "none") {
        Write-SbomLog WARN "GRYPE_FAIL_ON=none; Grype findings are reported without gating."
        return $null
    }
    if ($threshold -notin $SeverityOrder) {
        throw [PipelineError]::new("GRYPE_FAIL_ON must be one of $($SeverityOrder -join ', ') or none, not '$threshold'.")
    }
    $gated = $SeverityOrder[$SeverityOrder.IndexOf($threshold)..($SeverityOrder.Count - 1)]
    $failing = @($Findings | Where-Object { ([string]$_.vulnerability.severity).ToLowerInvariant() -in $gated } |
        ForEach-Object { "$($_.vulnerability.id) ($($_.artifact.name) $($_.artifact.version), $($_.vulnerability.severity))" } |
        Sort-Object -Unique)
    if ($failing.Count -eq 0) {
        Write-SbomLog SUCCESS "Grype Security Gate Passed (no findings at or above '$threshold')."
        return $null
    }
    foreach ($item in $failing) { Write-SbomLog ERROR "  Grype gate: $item" }
    return "GRYPE SECURITY GATE FAILED: $($failing.Count) finding(s) at or above '$threshold' in $ProjectName."
}

function Invoke-Project {
    param($Project, $Config, [string]$ManifestText, [string]$OutputBase, [bool]$SkipGrype, [string]$Source,
          [string]$ReportingMode,
          [System.Collections.IDictionary]$Record)
    $name = ([string]$Project["name"]).Trim()
    $buildPackages = $false
    $signedBuild = $null
    if ($null -ne $Project["build"]) {
        $buildPackages = $Project["build"]["packages"] -eq $true
        $hasSbomPath = -not [string]::IsNullOrWhiteSpace([string]$Project["build"]["sbomPath"])
        if ($hasSbomPath -ne (-not [string]::IsNullOrWhiteSpace([string]$Project["build"]["artifactPath"]))) {
            throw [PipelineError]::new("build.sbomPath and build.artifactPath must be set together.")
        }
        if ($hasSbomPath) { $signedBuild = @{ sbomPath = [string]$Project["build"]["sbomPath"]; artifactPath = [string]$Project["build"]["artifactPath"] } }
        if (-not $buildPackages -and $null -eq $signedBuild) {
            throw [PipelineError]::new("build must set packages = true and/or sbomPath with artifactPath.")
        }
    }
    if ($Project["sparsePaths"] -and ([string]$Project["vcs"]).Trim().ToLowerInvariant() -ne "svn") {
        throw [PipelineError]::new("sparsePaths only applies to SVN checkouts.")
    }
    $display = if ($Project["displayName"]) { ([string]$Project["displayName"]).Trim() } else { $name }
    $projectDir = [System.IO.Path]::Combine($WorkRoot, $name)
    $outputDir = [System.IO.Path]::Combine($OutputBase, $name)
    foreach ($dir in $projectDir, $outputDir) {
        if (Test-Path -LiteralPath $dir) { Remove-Item -LiteralPath $dir -Recurse -Force }
    }
    $null = New-Item -ItemType Directory -Force -Path $WorkRoot, $outputDir

    Set-PipelineStage -Record $Record -Name "checkout" -Status running
    $checkout = Invoke-Checkout -Project $Project -Config $Config -Destination $projectDir
    $patches = @(Invoke-CheckoutPatch -ProjectDir $projectDir -Vcs $checkout.Vcs -PatchPaths $Project["patches"] -BaseDir $Workspace)
    $Record.source = [ordered]@{ vcs = $checkout.Vcs; url = $checkout.RepoUrl; revision = $checkout.Revision }
    $Record.patches = @($patches | ForEach-Object { [ordered]@{ path = $_.Path; sha256 = $_.Sha256 } })
    Set-PipelineStage -Record $Record -Name "checkout" -Status completed
    if ($Project["build"] -and $Source -ne "vipm") {
        throw [PipelineError]::new("Configured build requires SBOM_SOURCE=vipm; manifest-only mode cannot build or verify artifacts.")
    }

    $tomlPath = [System.IO.Path]::Combine($projectDir, "vipm.toml")
    if (Test-Path -LiteralPath $tomlPath) { Remove-Item -LiteralPath $tomlPath -Force }
    $lvOverrideVersion = if ($Project["labviewVersion"]) { ([string]$Project["labviewVersion"]).Trim() } else { "" }
    $lvOverrideBitness = if ($Project["labviewBitness"]) { ([string]$Project["labviewBitness"]).Trim() } else { "" }
    $vipcPath = ([string]$Project["vipcPath"]).Trim()
    if ($lvOverrideVersion) {
        Write-VipmWorkspaceOverride -ProjectDir $projectDir -LabviewVersion $lvOverrideVersion -LabviewBitness $lvOverrideBitness
    }
    if ($Source -eq "vipm") {
        Write-SbomLog INFO "Initializing VIPM manifest at '$projectDir'..."
        Invoke-Vipm -Arguments @("init") -WorkingDirectory $projectDir
        if (-not (Test-Path -LiteralPath $tomlPath -PathType Leaf)) {
            throw [PipelineError]::new("vipm init completed without creating vipm.toml.")
        }
    }
    [System.IO.File]::WriteAllText($tomlPath, $ManifestText, [System.Text.UTF8Encoding]::new($false))
    Write-SbomLog SUCCESS "Deployed configured VIPM manifest."

    $manifest = Read-TomlTable -Path $tomlPath
    $projectTable = if ($manifest.ContainsKey("project")) { $manifest["project"] } else { @{} }
    $rootTable = $manifest[""]
    $pick = { param($Key, $Default)
        if ($projectTable[$Key]) { [string]$projectTable[$Key] } elseif ($rootTable[$Key]) { [string]$rootTable[$Key] } else { $Default } }
    $lvVersion = & $pick "labview-version" "2026"
    $lvBitness = & $pick "labview-bitness" "64"
    $productVersion = if ($projectTable["version"]) { [string]$projectTable["version"] } else { "1.0.0" }
    Write-SbomLog INFO "Resolved target environment from TOML: LabVIEW $lvVersion ($lvBitness-bit)."
    $requestedLv = "$lvVersion ($lvBitness-bit)"
    if ($lvOverrideVersion) {
        $lvVersion = $lvOverrideVersion
        if ($lvOverrideBitness) { $lvBitness = $lvOverrideBitness }
        Write-SbomLog INFO "Project override (.vipm/config.toml) selects LabVIEW $lvVersion ($lvBitness-bit) for every vipm command."
    }
    $buildProperties = [ordered]@{
        "vipm:labview-requested" = $requestedLv
        "vipm:labview-used"      = "$lvVersion ($lvBitness-bit)"
    }
    if ($vipcPath -and $Source -eq "vipm") { $buildProperties["vipm:vipc"] = $vipcPath }

    $safeDisplay = $display -replace '[^a-zA-Z0-9-]', '_'
    $prefix = [System.IO.Path]::Combine($outputDir, "$safeDisplay-$name")
    $sbomPath = "$prefix-sbom.json"
    $buildReport = $null
    $packageReports = @()
    if (-not $buildPackages) {
        Set-PipelineStage -Record $Record -Name "packages" -Status skipped -Reason "build.packages is not enabled."
    }

    if ($Source -eq "vipm") {
        if ($vipcPath) {
            $shortfall = Install-ProjectVipc -ProjectDir $projectDir -VipcPath $vipcPath `
                -LabviewVersion $lvVersion -LabviewBitness $lvBitness -QueryDir $outputDir
            if ($shortfall.Count -gt 0) { $buildProperties["vipm:vipc-not-installed"] = $shortfall -join ", " }
        }
        if ($buildPackages) {
            Set-PipelineStage -Record $Record -Name "packages" -Status running
            Install-ContainerBuildDependency -ProjectDir $projectDir -Manifest $manifest -Project $Project `
                -LabviewVersion $lvVersion -LabviewBitness $lvBitness
            $packagePlan = Resolve-PackageBuildPlan -TomlPath $tomlPath -ProjectDir $projectDir
            Write-SbomLog INFO "Building VI packages in dependency order: $(($packagePlan | ForEach-Object Target) -join ' -> ')."
            $packageReports = @(Invoke-PackageBuild -Plan $packagePlan -ProjectDir $projectDir -OutputDir $outputDir `
                -QueryDir $outputDir -LabviewVersion $lvVersion -LabviewBitness $lvBitness `
                -AllowPackageDrift ((Get-EnvValue ALLOW_PACKAGE_DRIFT "0") -ne "0"))
            Set-PipelineStage -Record $Record -Name "packages" -Status completed
        }
        Set-PipelineStage -Record $Record -Name "sbom" -Status running
        Write-SbomLog INFO "Generating vipm.lock from the deployed manifest..."
        Invoke-Vipm -Arguments @("lock") -WorkingDirectory $projectDir
        $lockPath = [System.IO.Path]::Combine($projectDir, "vipm.lock")
        if (-not (Test-Path -LiteralPath $lockPath -PathType Leaf)) {
            throw [PipelineError]::new("vipm lock completed without creating vipm.lock.")
        }
        $lockPackages = Read-LockPackage -Path $lockPath
        Copy-Item -LiteralPath $lockPath -Destination ([System.IO.Path]::Combine($outputDir, "vipm.lock"))
        if ($null -ne $signedBuild) {
            Write-SbomLog INFO "Building all VIPM targets and verifying Authenticode..."
            $buildReport = Invoke-ReportedVipmBuild -Build $signedBuild -ProjectDir $projectDir -OutputDir $outputDir `
                -LabviewVersion $lvVersion -LabviewBitness $lvBitness `
                -AllowPackageDrift ((Get-EnvValue ALLOW_PACKAGE_DRIFT "0") -ne "0")
        }

        Write-SbomLog INFO "Extracting VIPM SBOM for '$name'..."
        $sbomArgs = @("sbom", $tomlPath, "--format", "cyclonedx", "--schema-version", "1.5")
        if (-not $lvOverrideVersion) { $sbomArgs += @("--labview-version", $lvVersion, "--labview-bitness", $lvBitness) }
        $sbomArgs += @("--product-name", $name, "--product-version", $productVersion, "--output", $sbomPath)
        # NI's image ships its own LabVIEW patch level, which usually differs from the
        # manifest's pinned NIPM versions; the nipkg deep scan records what is installed.
        if ((Get-EnvValue ALLOW_PACKAGE_DRIFT "0") -ne "0") { $sbomArgs += "--allow-package-drift" }
        Invoke-Vipm -Arguments $sbomArgs -WorkingDirectory $projectDir
        $sbom = ConvertFrom-Json -InputObject ([System.IO.File]::ReadAllText($sbomPath)) -AsHashtable -Depth 100
    } else {
        Set-PipelineStage -Record $Record -Name "sbom" -Status running
        Write-SbomLog WARN ("SBOM_SOURCE=manifest: building the base SBOM from vipm.toml without VIPM " +
                        "(no vipm lock/sbom; VI-level dependency scanning is not performed).")
        $lockPackages = @()
        $sbom = ConvertTo-ManifestSbom -Manifest $manifest -Name $name -Version $productVersion -LvVersion $lvVersion -LvBitness $lvBitness
    }

    $baseCount = @($sbom["components"] | Where-Object { $_ }).Count
    $added = Add-SbomEnrichment -Sbom $sbom -Manifest $manifest -LockPackages $lockPackages
    Add-SbomProperty -Sbom $sbom -Properties ([ordered]@{
        "vcs:type"     = $checkout.Vcs
        "vcs:url"      = $checkout.RepoUrl
        "vcs:revision" = $checkout.Revision
    })
    Add-SbomProperty -Sbom $sbom -Properties $buildProperties
    foreach ($patch in $patches) {
        Add-SbomProperty -Sbom $sbom -Properties @{ "checkout:patch" = "$($patch.Path) sha256:$($patch.Sha256)" }
    }
    if ($packageReports.Count -gt 0) {
        Add-PackageBuildComponent -Sbom $sbom -Reports $packageReports -RepoUrl $checkout.RepoUrl -Revision $checkout.Revision
        Write-SbomLog SUCCESS "Included $($packageReports.Count) built package(s): $(($packageReports | ForEach-Object { "$($_.Name) $($_.Version)" }) -join ', ')."
    }
    if ($null -ne $buildReport) {
        Add-VerifiedBuildComponent -Sbom $sbom -Report $buildReport
        Write-SbomLog SUCCESS "Included build '$($buildReport.Bom.metadata.component.name)' with verified Authenticode signature."
    }
    [System.IO.File]::WriteAllText($sbomPath, (ConvertTo-Json -InputObject $sbom -Depth 100) + "`n", [System.Text.UTF8Encoding]::new($false))
    $total = @($sbom["components"]).Count
    Write-SbomLog SUCCESS "Enriched SBOM written to $(Get-RelativeOutputPath $sbomPath) ($baseCount from $Source, $total total components)."
    Set-PipelineStage -Record $Record -Name "sbom" -Status completed

    $details = "$total components ($added from nipkg)"
    $grypeGateError = $null
    if ($SkipGrype) {
        Write-SbomLog WARN "SKIP_GRYPE=1; skipping the vulnerability scan."
        Set-PipelineStage -Record $Record -Name "grype" -Status skipped -Reason "SKIP_GRYPE=1."
    } else {
        $reportPath = "$prefix-vuln-report.txt"
        $jsonReportPath = "$prefix-vuln-report.json"
        Write-SbomLog INFO "Scanning the enriched SBOM with Grype..."
        Set-PipelineStage -Record $Record -Name "grype" -Status running
        Invoke-Native -Arguments @("grype", "sbom:$sbomPath", "--quiet", "-o", "table=$reportPath", "-o", "json=$jsonReportPath")
        $report = ConvertFrom-Json -InputObject ([System.IO.File]::ReadAllText($jsonReportPath)) -Depth 100
        $findings = @($report.matches | Where-Object { $_ })
        $breakdown = ($findings | Group-Object { $_.vulnerability.severity } | Sort-Object Name |
            ForEach-Object { "$($_.Name): $($_.Count)" }) -join ", "
        Write-SbomLog METRIC "Grype findings: $($findings.Count) ($(if ($breakdown) { $breakdown } else { 'none' }))."
        Write-SbomLog SUCCESS "Grype reports written to $(Get-RelativeOutputPath $reportPath) (+ .json)."
        $details += ", $($findings.Count) Grype findings"
        $grypeGateError = Test-GrypeGate -Findings $findings -ProjectName $name
        $grypeGate = if ($grypeGateError) { "failed" }
            elseif ((Get-EnvValue GRYPE_FAIL_ON "high").ToLowerInvariant() -eq "none") { "not-gated" }
            else { "passed" }
        Set-PipelineStage -Record $Record -Name "grype" -Status completed -Gate $grypeGate
    }

    if ($ReportingMode -eq "hosted-grype") {
        Write-SbomLog WARN "DEPENDENCY_TRACK_MODE=hosted-grype; skipping the Dependency-Track upload and gate."
        Set-PipelineStage -Record $Record -Name "dependencyTrack" -Status skipped -Reason "DEPENDENCY_TRACK_MODE=hosted-grype."
    } else {
        $dtName = Get-EnvValue DEPENDENCY_TRACK_PROJECT_NAME "${display}:$name-windows-container"
        Set-PipelineStage -Record $Record -Name "dependencyTrack" -Status running
        $details += ", " + (Publish-DependencyTrack -Sbom $sbom -SbomPath $sbomPath -ProjectName $dtName `
            -ProjectVersion $productVersion -ExportPath "$prefix-dt-findings.json" -LvVersion $lvVersion -LvBitness $lvBitness `
            -Record $Record)
    }
    # Raised after the upload so Dependency-Track still receives the inventory.
    if ($grypeGateError) { throw [PipelineError]::new($grypeGateError) }
    return $details
}

function Invoke-Main {
    $reportingMode = Get-EnvValue DEPENDENCY_TRACK_MODE "full"
    $reportingMode = Test-DependencyTrackReadiness -Mode $reportingMode `
        -BaseUrl (Get-EnvValue DEPENDENCY_TRACK_URL) `
        -ApiKey (Get-EnvValue DEPENDENCY_TRACK_API_KEY) `
        -SecurityGate (Get-EnvValue DEPENDENCY_TRACK_SECURITY_GATE "1")
    $outputRelative = Get-EnvValue OUTPUT_DIR "output/windows"
    $outputBase = Get-WorkspacePath -Relative $outputRelative -Name OUTPUT_DIR
    $skipGrype = (Get-EnvValue SKIP_GRYPE "0") -eq "1"
    $source = (Get-EnvValue SBOM_SOURCE "vipm").ToLowerInvariant()
    if ($source -notin "vipm", "manifest") { throw [PipelineError]::new("SBOM_SOURCE must be 'vipm' or 'manifest', not '$source'.") }

    Write-SbomLog INFO "=================================================="
    Write-SbomLog INFO "Windows Container SBOM Pipeline"
    Write-SbomLog INFO "Base image: $(Get-EnvValue LABVIEW_IMAGE 'unknown')"
    Write-SbomLog INFO "SBOM source: $source"
    if ($source -eq "vipm") { Write-SbomLog INFO "VIPM:       $((Invoke-Native -Arguments @('vipm', '--version') -Capture).Trim())" }
    Write-SbomLog INFO "Output:     $outputRelative"
    Write-SbomLog INFO "=================================================="

    $config = Get-PipelineConfig
    $projects = @($config["projects"])
    $enabled = [string[]]@($projects | Where-Object { $_["enabled"] -ne $false } | ForEach-Object { ([string]$_["name"]).Trim() })
    $manifests = Get-VipmManifest -EnabledProjects $enabled

    $summary = [System.Collections.Generic.List[object]]::new()
    if (-not $skipGrype) {
        try { Copy-GrypeDatabase -Direction Restore }
        catch { Write-SbomLog WARN "Could not restore the Grype database cache; Grype will download it: $($_.Exception.Message)" }
    }
    for ($index = 0; $index -lt $projects.Count; $index++) {
        $project = $projects[$index]
        $name = ([string]$project["name"]).Trim()
        Write-SbomLog INFO "Project [$($index + 1)/$($projects.Count)]: $name"
        if ($project["enabled"] -eq $false) {
            $summary.Add([pscustomobject]@{ Name = $name; Status = "SKIPPED"; Details = "Disabled in config." })
            continue
        }
        $record = New-PipelineRunRecord -Project $name -Pipeline "windows-container" -ReportingMode $reportingMode
        try {
            $manifestText = $manifests[$name]
            if ($manifestText -isnot [string] -or [string]::IsNullOrWhiteSpace($manifestText)) {
                throw [PipelineError]::new("No VIPM manifest supplied for project '$name'.")
            }
            $details = Invoke-Project -Project $project -Config $config -ManifestText $manifestText `
                -OutputBase $outputBase -SkipGrype $skipGrype -Source $source -ReportingMode $reportingMode -Record $record
            Complete-PipelineRunRecord -Record $record
            $summary.Add([pscustomobject]@{ Name = $name; Status = "SUCCESS"; Details = $details })
        } catch {
            Write-SbomLog ERROR "Project '$name' failed: $($_.Exception.Message)"
            Complete-PipelineRunRecord -Record $record -Failure $_.Exception.Message
            $summary.Add([pscustomobject]@{ Name = $name; Status = "FAILED"; Details = $_.Exception.Message })
        }
        try { Write-PipelineRunRecord -Record $record -OutputDir ([System.IO.Path]::Combine($outputBase, $name)) }
        catch { Write-SbomLog ERROR "Could not write pipeline-run.json for '$name': $($_.Exception.Message)" }
    }
    if (-not $skipGrype) {
        try { Copy-GrypeDatabase -Direction Save }
        catch { Write-SbomLog WARN "Could not save the Grype database cache: $($_.Exception.Message)" }
    }

    Write-SbomLog INFO "=================================================="
    foreach ($entry in $summary) {
        $level = switch ($entry.Status) { "SUCCESS" { "SUCCESS" } "SKIPPED" { "WARN" } default { "ERROR" } }
        Write-SbomLog $level "$($entry.Name): $($entry.Status) - $($entry.Details)"
    }
    if (@($summary | Where-Object Status -eq "FAILED").Count -gt 0) { return 1 }
    return 0
}

try {
    exit (Invoke-Main)
} catch {
    Write-SbomLog ERROR $_.Exception.Message
    exit 1
}
