function Invoke-VipmBuild {
    # When vipm build launches LabVIEW itself, its VI Server call can reach LabVIEW before
    # LabVIEW is ready and fail with the opaque code 1130. The same build succeeds once LabVIEW
    # is running, so retry only that failure, and only while the LabVIEW it launched is open.
    param([string[]]$Arguments, [string]$WorkingDirectory)
    try {
        Invoke-Vipm -Arguments $Arguments -WorkingDirectory $WorkingDirectory -ShowProgress
    } catch {
        $message = $_.Exception.Message
        if ($message -notmatch '\(code 1130\)' -or -not (Get-Process -Name LabVIEW -ErrorAction SilentlyContinue)) { throw }
        Write-PipelineLog -Message "vipm build failed with code 1130 right after launching LabVIEW; retrying once with LabVIEW already running." -Level WARN
        try {
            Invoke-Vipm -Arguments $Arguments -WorkingDirectory $WorkingDirectory -ShowProgress
        } catch {
            throw "$($_.Exception.Message) (Failed again after a retry with LabVIEW already running; the error above is App Builder's own error.)"
        }
    }
}

function Invoke-ReportedVipmBuild {
    param(
        [System.Collections.IDictionary]$Build,
        [string]$ProjectDir,
        [string]$OutputDir,
        [string]$LabviewVersion,
        [string]$LabviewBitness,
        [bool]$AllowPackageDrift
    )
    $paths = @{}
    $previous = @{}
    foreach ($key in "sbomPath", "artifactPath") {
        if ([string]::IsNullOrWhiteSpace([string]$Build[$key])) { throw "build.$key must be a non-blank path." }
        $paths[$key] = [System.IO.Path]::GetFullPath([System.IO.Path]::Combine($ProjectDir, [string]$Build[$key]))
        $previous[$key] = if (Test-Path -LiteralPath $paths[$key] -PathType Leaf) {
            (Get-Item -LiteralPath $paths[$key]).LastWriteTimeUtc.Ticks
        } else { $null }
    }
    if ($paths.sbomPath -eq $paths.artifactPath) { throw "build.sbomPath and build.artifactPath must be different files." }
    $arguments = @("build", "--all", "--labview-version", $LabviewVersion, "--labview-bitness", $LabviewBitness)
    if ($AllowPackageDrift) { $arguments += "--allow-package-drift" }
    Write-RunningLabviewWarning
    Invoke-VipmBuild -Arguments $arguments -WorkingDirectory $ProjectDir
    foreach ($key in "sbomPath", "artifactPath") {
        if (-not (Test-Path -LiteralPath $paths[$key] -PathType Leaf)) { throw "vipm build produced no build.$key at '$($paths[$key])'." }
        if ($null -ne $previous[$key] -and (Get-Item -LiteralPath $paths[$key]).LastWriteTimeUtc.Ticks -le $previous[$key]) {
            throw "vipm build did not refresh build.$key at '$($paths[$key])'; refusing stale build output."
        }
    }
    $report = Read-VerifiedBuildSbom -SbomPath $paths.sbomPath -ArtifactPath $paths.artifactPath
    $archiveDir = [System.IO.Path]::Combine($OutputDir, "build")
    $null = New-Item -ItemType Directory -Path $archiveDir -Force
    $archivePath = [System.IO.Path]::Combine($archiveDir, [System.IO.Path]::GetFileName($paths.sbomPath))
    if ($archivePath -ne $paths.sbomPath) { Copy-Item -LiteralPath $paths.sbomPath -Destination $archivePath -Force }
    $report.Signature | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath ([System.IO.Path]::Combine($archiveDir, "authenticode.json")) -Encoding utf8
    return $report
}

function Read-VerifiedBuildSbom {
    param([string]$SbomPath, [string]$ArtifactPath)
    $bom = ConvertFrom-Json -InputObject ([System.IO.File]::ReadAllText($SbomPath)) -AsHashtable -Depth 100
    if ($bom.bomFormat -ne "CycloneDX" -or -not $bom.metadata.component.name -or
        -not $bom.metadata.component.version -or -not $bom.metadata.component["bom-ref"]) {
        throw "Build SBOM must be CycloneDX JSON with a named, versioned metadata.component and bom-ref."
    }
    $signature = Get-AuthenticodeSignature -LiteralPath $ArtifactPath
    if ($signature.Status -ne "Valid" -or $null -eq $signature.SignerCertificate) {
        throw "Build artifact '$ArtifactPath' failed Authenticode verification: $($signature.Status). $($signature.StatusMessage)"
    }
    return @{
        Bom = $bom
        ArtifactHash = (Get-FileHash -LiteralPath $ArtifactPath -Algorithm SHA256).Hash.ToLowerInvariant()
        SbomHash = (Get-FileHash -LiteralPath $SbomPath -Algorithm SHA256).Hash.ToLowerInvariant()
        ArtifactName = [System.IO.Path]::GetFileName($ArtifactPath)
        Signature = [ordered]@{
            status = [string]$signature.Status
            signerSubject = $signature.SignerCertificate.Subject
            signerThumbprint = $signature.SignerCertificate.Thumbprint
            timestampSubject = if ($signature.TimeStamperCertificate) { $signature.TimeStamperCertificate.Subject } else { "" }
        }
    }
}

function Add-VerifiedBuildComponent {
    param([System.Collections.IDictionary]$Sbom, [System.Collections.IDictionary]$Report)
    $buildBom = $Report.Bom
    $buildComponent = $buildBom.metadata.component
    $buildRef = "build:$($buildComponent['bom-ref'])"
    $rootRef = [string]$Sbom.metadata.component["bom-ref"]
    if (-not $rootRef) { throw "Project SBOM metadata.component is missing bom-ref." }
    $components = [System.Collections.Generic.List[object]]::new()
    $knownRefs = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::Ordinal)
    [void]$knownRefs.Add($rootRef)
    $purls = @{}
    foreach ($component in @($Sbom.components)) {
        if (-not $component) { continue }
        $components.Add($component)
        if (-not $component["bom-ref"] -or -not $knownRefs.Add([string]$component["bom-ref"])) { throw "Duplicate or missing project component bom-ref." }
        if ($component.purl) { $purls[[System.Uri]::UnescapeDataString([string]$component.purl)] = [string]$component["bom-ref"] }
    }
    if (-not $knownRefs.Add($buildRef)) { throw "Build component bom-ref '$buildRef' already exists." }
    $oldRoot = [string]$buildComponent["bom-ref"]
    $refMap = @{ $oldRoot = $buildRef }
    $buildComponent["bom-ref"] = $buildRef
    $buildComponent.hashes = @(@{ alg = "SHA-256"; content = $Report.ArtifactHash })
    $buildComponent.properties = @($buildComponent.properties | Where-Object { $_ }) + @(
        @{ name = "build:artifact"; value = $Report.ArtifactName },
        @{ name = "build:sbom:sha256"; value = $Report.SbomHash },
        @{ name = "build:authenticode:status"; value = $Report.Signature.status },
        @{ name = "build:authenticode:signer"; value = $Report.Signature.signerSubject },
        @{ name = "build:authenticode:thumbprint"; value = $Report.Signature.signerThumbprint },
        @{ name = "build:authenticode:timestamp-signer"; value = $Report.Signature.timestampSubject }
    )
    $components.Add($buildComponent)
    foreach ($component in @($buildBom.components)) {
        if (-not $component) { continue }
        $oldRef = [string]$component["bom-ref"]
        if (-not $oldRef -or $refMap.ContainsKey($oldRef)) { throw "Duplicate or missing build component bom-ref '$oldRef'." }
        $purl = if ($component.purl) { [System.Uri]::UnescapeDataString([string]$component.purl) } else { "" }
        if ($purl -and $purls.ContainsKey($purl)) {
            $refMap[$oldRef] = $purls[$purl]
        } else {
            $newRef = "build:$oldRef"
            if (-not $knownRefs.Add($newRef)) { throw "Duplicate build component bom-ref '$newRef'." }
            $refMap[$oldRef] = $newRef
            $component["bom-ref"] = $newRef
            $components.Add($component)
            if ($purl) { $purls[$purl] = $newRef }
        }
    }
    $graph = @{}
    foreach ($node in @($Sbom.dependencies)) {
        if (-not $node) { continue }
        if (-not $knownRefs.Contains([string]$node.ref)) { throw "Unknown project dependency ref '$($node.ref)'." }
        if (-not $graph.ContainsKey([string]$node.ref)) { $graph[[string]$node.ref] = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::Ordinal) }
        foreach ($child in @($node.dependsOn)) {
            if (-not $knownRefs.Contains([string]$child)) { throw "Unknown project dependency '$child'." }
            [void]$graph[[string]$node.ref].Add([string]$child)
        }
    }
    foreach ($node in @($buildBom.dependencies)) {
        if (-not $node) { continue }
        if (-not $refMap.ContainsKey([string]$node.ref)) { throw "Unknown build dependency ref '$($node.ref)'." }
        $mapped = $refMap[[string]$node.ref]
        if (-not $graph.ContainsKey($mapped)) { $graph[$mapped] = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::Ordinal) }
        foreach ($child in @($node.dependsOn)) {
            if (-not $refMap.ContainsKey([string]$child)) { throw "Unknown build dependency '$child'." }
            [void]$graph[$mapped].Add($refMap[[string]$child])
        }
    }
    if (-not $graph.ContainsKey($rootRef)) { $graph[$rootRef] = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::Ordinal) }
    [void]$graph[$rootRef].Add($buildRef)
    $Sbom.components = $components.ToArray()
    $Sbom.dependencies = @(foreach ($ref in $graph.Keys) { @{ ref = $ref; dependsOn = @($graph[$ref] | Sort-Object) } })
}

function Test-CheckoutRelativePath {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path) -or [System.IO.Path]::IsPathRooted($Path) -or $Path -match '^([\\/]|[A-Za-z]:)') { return $false }
    $segments = ($Path -replace '\\', '/').Split('/')
    return -not ($segments -contains ".." -or $segments -contains "")
}

function Get-SvnSparseUpdateStep {
    <#
    Returns the `svn update --set-depth` steps that materialize only the
    configured directories of a `--depth empty` checkout: every parent becomes
    an empty directory once, and each configured path is fetched fully.
    #>
    param([string[]]$Paths)
    $steps = [System.Collections.Generic.List[object]]::new()
    $seen = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($path in $Paths) {
        if (-not (Test-CheckoutRelativePath $path)) { throw "sparsePaths entry '$path' must be a relative path inside the checkout." }
        $segments = ($path -replace '\\', '/').Trim('/').Split('/')
        for ($i = 0; $i -lt $segments.Count; $i++) {
            $prefix = $segments[0..$i] -join '/'
            $depth = if ($i -eq $segments.Count - 1) { "infinity" } else { "empty" }
            if ($depth -eq "empty" -and -not $seen.Add($prefix)) { continue }
            if ($depth -eq "infinity") { [void]$seen.Add($prefix) }
            $steps.Add([PSCustomObject]@{ Path = $prefix; Depth = $depth })
        }
    }
    return $steps.ToArray()
}

function Invoke-CheckoutPatch {
    <#
    Applies unified diffs (paths relative to the checkout root) to a fresh
    checkout with `svn patch` or `git apply`. `svn patch` exits 0 even when
    hunks are rejected or targets are missing, so its report is checked too.
    #>
    param(
        [Parameter(Mandatory)][string]$ProjectDir,
        [Parameter(Mandatory)][ValidateSet("svn", "git")][string]$Vcs,
        [string[]]$PatchPaths,
        [Parameter(Mandatory)][string]$BaseDir
    )
    foreach ($patch in @($PatchPaths | Where-Object { $_ })) {
        $patchPath = if ([System.IO.Path]::IsPathRooted($patch)) { $patch } else { [System.IO.Path]::GetFullPath([System.IO.Path]::Combine($BaseDir, $patch)) }
        if (-not (Test-Path -LiteralPath $patchPath -PathType Leaf)) {
            throw "Configuration Error: patch file '$patch' was not found (resolved to '$patchPath')."
        }
        Write-PipelineLog -Message "Applying checkout patch '$patch'..." -Level INFO
        $output = if ($Vcs -eq "svn") {
            & svn patch --non-interactive $patchPath $ProjectDir 2>&1
        } else {
            & git -C $ProjectDir apply --whitespace=nowarn $patchPath 2>&1
        }
        $exitCode = $LASTEXITCODE
        $lines = @($output | ForEach-Object { "$_".TrimEnd() } | Where-Object { $_ })
        foreach ($line in $lines) { Write-PipelineLog -Message "  $Vcs patch | $line" -Level INFO }
        $failed = $exitCode -ne 0
        if ($Vcs -eq "svn") {
            $failed = $failed -or
                ($lines | Where-Object { $_ -match '^(C|Skipped)\s|rejected hunk|Summary of conflicts' }) -or
                -not ($lines | Where-Object { $_ -match '^[UAGD]\s' })
        }
        if ($failed) {
            throw "Patch '$patch' did not apply cleanly to '$ProjectDir' (exit code $exitCode). Regenerate it against the configured revision."
        }
        [PSCustomObject]@{ Path = $patch; Sha256 = (Get-FileHash -LiteralPath $patchPath -Algorithm SHA256).Hash.ToLowerInvariant() }
    }
}

function Read-VipmBuildTarget {
    # Reads the [build.<name>] tables of a vipm.toml (string and string-array values).
    param([string]$TomlPath)
    $targets = [ordered]@{}
    $current = $null
    foreach ($rawLine in [System.IO.File]::ReadAllLines($TomlPath)) {
        $line = ($rawLine -replace '\s+#[^"]*$', '').Trim()
        if ($line -match '^\[\s*build\.("?)([^"\]]+)\1\s*\]$') {
            $current = [ordered]@{ name = $Matches[2].Trim() }
            $targets[$current.name] = $current
        } elseif ($line -match '^\[') {
            $current = $null
        } elseif ($null -ne $current -and $line -match '^([A-Za-z0-9_-]+)\s*=\s*(.+)$') {
            $key = $Matches[1]
            $value = $Matches[2].Trim()
            if ($value -match '^\[(.*)\]$') {
                $current[$key] = @([regex]::Matches($Matches[1], '"([^"]*)"|''([^'']*)''') | ForEach-Object {
                        if ($_.Groups[1].Success) { $_.Groups[1].Value } else { $_.Groups[2].Value } })
            } else {
                $current[$key] = $value.Trim('"', "'")
            }
        }
    }
    return $targets
}

function Resolve-PackageBuildPlan {
    <#
    Plans build.packages: every type = "vipb" target of vipm.toml, ordered so
    each target follows the targets it depends-on (declaration order otherwise).
    `vipm build --all` builds in that order too but never installs its outputs,
    so a VIPB whose sources call a sibling package's VIs fails unless the
    pipeline builds and installs one target at a time.
    #>
    param([string]$TomlPath, [string]$ProjectDir)
    $definitions = Read-VipmBuildTarget -TomlPath $TomlPath
    $tomlDir = [System.IO.Path]::GetDirectoryName([System.IO.Path]::GetFullPath($TomlPath))
    $vipbTargets = @($definitions.Keys | Where-Object { $definitions[$_].type -eq "vipb" })
    if ($vipbTargets.Count -eq 0) { throw "build.packages is set, but vipm.toml has no [build.<name>] target with type = `"vipb`"." }
    $state = @{}
    $order = [System.Collections.Generic.List[string]]::new()
    $visit = {
        param([string]$Target, [string[]]$Chain)
        if ($state[$Target] -eq "done") { return }
        if ($state[$Target] -eq "visiting") { throw "vipm.toml build targets have a depends-on cycle: $(($Chain + $Target) -join ' -> ')." }
        $state[$Target] = "visiting"
        foreach ($dependency in @($definitions[$Target]['depends-on'] | Where-Object { $_ })) {
            if (-not $definitions.Contains($dependency)) { throw "Build target '$Target' depends-on unknown target '$dependency'." }
            if ($definitions[$dependency].type -ne "vipb") {
                throw "Build target '$Target' depends-on '$dependency', which is not a vipb target; build.packages only builds VI packages."
            }
            & $visit $dependency ($Chain + $Target)
        }
        $state[$Target] = "done"
        $order.Add($Target)
    }
    foreach ($target in $vipbTargets) { & $visit $target @() }
    $plan = [System.Collections.Generic.List[object]]::new()
    foreach ($target in $order) {
        $definition = $definitions[$target]
        if (-not (Test-CheckoutRelativePath ([string]$definition.path))) { throw "Build target '$target' path must be relative to vipm.toml without '..'." }
        $vipbPath = [System.IO.Path]::GetFullPath([System.IO.Path]::Combine($tomlDir, [string]$definition.path))
        if (-not (Test-Path -LiteralPath $vipbPath -PathType Leaf)) { throw "Build target '$target' VIPB '$($definition.path)' was not found in the checkout." }
        [xml]$vipb = [System.IO.File]::ReadAllText($vipbPath)
        $general = $vipb.VI_Package_Builder_Settings.Library_General_Settings
        $packageName = [string]$general.Package_File_Name
        if (-not $packageName) { throw "VIPB '$($definition.path)' has no Package_File_Name." }
        $plan.Add([PSCustomObject]@{
                Target = $target
                VipbPath = $vipbPath
                VipbRelativePath = (([string]$definition.path) -replace '\\', '/')
                PackageName = $packageName
                VipbLabviewVersion = [string]$general.Package_LabVIEW_Version
                OutputDir = [System.IO.Path]::Combine([System.IO.Path]::GetFullPath($ProjectDir), ".sbom-build", $target)
            })
    }
    return $plan.ToArray()
}

function Write-VipbOutputFolder {
    # Points the VIPB at a dedicated, empty output folder so the built .vip is unambiguous.
    param([string]$VipbPath, [string]$OutputDir)
    $text = [System.IO.File]::ReadAllText($VipbPath)
    $replacement = "<Library_Output_Folder>$([System.Security.SecurityElement]::Escape($OutputDir).Replace('$', '$$'))</Library_Output_Folder>"
    $pattern = [regex]'<Library_Output_Folder\s*/>|<Library_Output_Folder>[^<]*</Library_Output_Folder>'
    if (-not $pattern.IsMatch($text)) { throw "VIPB '$VipbPath' has no Library_Output_Folder setting." }
    $updated = $pattern.Replace($text, $replacement, 1)
    [System.IO.File]::WriteAllText($VipbPath, $updated, [System.Text.UTF8Encoding]::new($false))
    if (Test-Path -LiteralPath $OutputDir) { Remove-Item -LiteralPath $OutputDir -Recurse -Force }
    $null = New-Item -ItemType Directory -Path $OutputDir -Force
}

function Write-RunningLabviewWarning {
    # LabVIEW instances left over from earlier builds (or other versions) make vipm builds fail with 1055/1130.
    $running = @(Get-Process -Name LabVIEW -ErrorAction SilentlyContinue)
    if ($running.Count -gt 0) {
        $list = ($running | ForEach-Object { "PID $($_.Id) $(try { $_.Path } catch { '' })" }) -join '; '
        Write-PipelineLog -Message "LabVIEW is already running ($list). vipm build drives its own LabVIEW instance; stale instances commonly cause build errors 1055/1130. Close them if the build fails." -Level WARN
    }
}

function Invoke-StreamingProcess {
    <#
    Runs an executable and passes each stdout/stderr line to $OnLine as
    ($line, $isStderr) while it runs; returns the exit code. Reading stops once
    the process has exited and its pipes have been quiet for $DrainSeconds,
    instead of waiting for end-of-file: `vipm build` launches LabVIEW, which
    inherits the output pipes and keeps them open long after vipm exits.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$ArgumentList = @(),
        [string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][scriptblock]$OnLine,
        [double]$DrainSeconds = 2
    )
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new($FilePath)
    foreach ($argument in $ArgumentList) { $startInfo.ArgumentList.Add($argument) }
    if ($WorkingDirectory) { $startInfo.WorkingDirectory = $WorkingDirectory }
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.StandardOutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $startInfo.StandardErrorEncoding = [System.Text.UTF8Encoding]::new($false)
    $process = [System.Diagnostics.Process]::Start($startInfo)
    try {
        $streams = @(
            @{ Reader = $process.StandardOutput; IsStderr = $false; Task = $null; Done = $false },
            @{ Reader = $process.StandardError; IsStderr = $true; Task = $null; Done = $false }
        )
        $quiet = [System.Diagnostics.Stopwatch]::new()
        while ($true) {
            $readAny = $false
            foreach ($stream in $streams) {
                if ($stream.Done) { continue }
                if ($null -eq $stream.Task) { $stream.Task = $stream.Reader.ReadLineAsync() }
                if (-not $stream.Task.IsCompleted) { continue }
                $line = $stream.Task.GetAwaiter().GetResult()
                $stream.Task = $null
                $readAny = $true
                if ($null -eq $line) { $stream.Done = $true } else { & $OnLine $line $stream.IsStderr }
            }
            if (-not ($streams | Where-Object { -not $_.Done })) { break }
            if ($readAny) { $quiet.Reset(); continue }
            if ($process.HasExited) {
                if (-not $quiet.IsRunning) { $quiet.Start() }
                if ($quiet.Elapsed.TotalSeconds -ge $DrainSeconds) {
                    Write-PipelineLog -Message "$([System.IO.Path]::GetFileName($FilePath)) exited but a child process (for example LabVIEW) still holds its output; continuing without waiting for it." -Level DEBUG
                    break
                }
            }
            Start-Sleep -Milliseconds 50
        }
        $process.WaitForExit()
        return $process.ExitCode
    } finally {
        $process.Dispose()
    }
}

function Read-VipPackageSpec {
    # Reads name, version and Requires from the `spec` entry inside a built .vip (zip).
    param([string]$VipPath)
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($VipPath)
    try {
        $entry = $archive.Entries | Where-Object { $_.FullName -eq "spec" } | Select-Object -First 1
        if ($null -eq $entry) { throw "Package '$VipPath' has no spec entry." }
        $reader = [System.IO.StreamReader]::new($entry.Open())
        try { $text = $reader.ReadToEnd() } finally { $reader.Dispose() }
    } finally {
        $archive.Dispose()
    }
    $values = @{}
    foreach ($line in $text -split "\r?\n") {
        if ($line -match '^([A-Za-z_ ]+)="(.*)"\s*$' -and -not $values.ContainsKey($Matches[1])) { $values[$Matches[1]] = $Matches[2] }
    }
    if (-not $values.Name -or -not $values.Version) { throw "Package '$VipPath' spec has no Name or Version." }
    $requires = @(([string]$values.Requires).Split(',') | ForEach-Object {
            if ($_ -match '^\s*([A-Za-z0-9_.\-]+)') { $Matches[1] } } | Where-Object { $_ })
    return [PSCustomObject]@{ Name = $values.Name; Version = $values.Version; Requires = $requires }
}

function Find-BuiltVipPackage {
    param([string]$OutputDir, [string]$PackageName)
    $candidates = @(Get-ChildItem -LiteralPath $OutputDir -Filter "*.vip" -File -ErrorAction SilentlyContinue | Where-Object {
            $_.BaseName -like "$PackageName-*"
        })
    if ($candidates.Count -ne 1) {
        throw "vipm build must produce exactly one '$PackageName-*.vip' in '$OutputDir' (found $($candidates.Count))."
    }
    return $candidates[0].FullName
}

function Write-PackageBuildReport {
    param([object]$Report, [string]$ArchiveDir)
    $Report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath ([System.IO.Path]::Combine($ArchiveDir, "$($Report.Name).json")) -Encoding utf8
}

function Invoke-BoundedDiagnosticJob {
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute("PSUseShouldProcessForStateChangingFunctions", "", Justification = "Creates only a temporary local job and always stops and removes it.")]
    param(
        [Parameter(Mandatory)]
        [scriptblock]$ScriptBlock,
        [object[]]$ArgumentList = @(),
        [ValidateRange(1, 15)]
        [int]$TimeoutSeconds = 5
    )

    $stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
    $job = $null
    try {
        $job = Start-Job -ScriptBlock $ScriptBlock -ArgumentList $ArgumentList -ErrorAction Stop
        $remainingSeconds = [Math]::Max(0, $TimeoutSeconds - $stopwatch.Elapsed.TotalSeconds)
        if ($remainingSeconds -gt 0) {
            $null = Wait-Job -Job $job -Timeout $remainingSeconds
        }
        if ($job.State -eq "Running" -or $job.State -eq "NotStarted" -or
            $stopwatch.Elapsed.TotalSeconds -ge $TimeoutSeconds) {
            Stop-Job -Job $job -ErrorAction SilentlyContinue
            return [pscustomobject]@{
                Status = "timed-out"
                Result = @()
                DurationMilliseconds = [int]$stopwatch.ElapsedMilliseconds
                ErrorType = $null
            }
        }
        if ($job.State -ne "Completed") {
            $jobError = @($job.ChildJobs[0].Error | Select-Object -First 1)
            return [pscustomobject]@{
                Status = "failed"
                Result = @()
                DurationMilliseconds = [int]$stopwatch.ElapsedMilliseconds
                ErrorType = if ($jobError.Count -gt 0) { $jobError[0].Exception.GetType().Name } else { "DiagnosticJobFailed" }
            }
        }

        $result = @(Receive-Job -Job $job -ErrorAction Stop)
        return [pscustomobject]@{
            Status = "captured"
            Result = $result
            DurationMilliseconds = [int]$stopwatch.ElapsedMilliseconds
            ErrorType = $null
        }
    } catch {
        return [pscustomobject]@{
            Status = "failed"
            Result = @()
            DurationMilliseconds = [int]$stopwatch.ElapsedMilliseconds
            ErrorType = $_.Exception.GetType().Name
        }
    } finally {
        if ($job) {
            if ($job.State -in @("Running", "NotStarted")) {
                Stop-Job -Job $job -ErrorAction SilentlyContinue
            }
            Remove-Job -Job $job -Force -ErrorAction SilentlyContinue
        }
        $stopwatch.Stop()
    }
}

function Get-VipmDebugLogState {
    param(
        [Parameter(Mandatory)]
        [string]$Path
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return [ordered]@{ Status = "missing"; Bytes = 0; LastWriteTimeUtc = $null; LinesRead = 0; ErrorLines = 0; WarningLines = 0; TimeoutLines = 0; ExceptionLines = 0 }
    }

    try {
        $file = Get-Item -LiteralPath $Path -ErrorAction Stop
        $state = [ordered]@{
            Status = "metadata-only"
            Bytes = [long]$file.Length
            LastWriteTimeUtc = $file.LastWriteTimeUtc.ToString("o")
            LinesRead = 0
            ErrorLines = 0
            WarningLines = 0
            TimeoutLines = 0
            ExceptionLines = 0
        }
        if ($file.Length -gt 65536) {
            $state.Status = "summary-skipped-size-limit"
            return $state
        }

        foreach ($line in Get-Content -LiteralPath $Path -ErrorAction Stop) {
            $state.LinesRead++
            if ($line -match "(?i)\b(error|failed|fatal)\b") { $state.ErrorLines++ }
            if ($line -match "(?i)\bwarning\b") { $state.WarningLines++ }
            if ($line -match "(?i)\btimeout|timed out|made no progress\b") { $state.TimeoutLines++ }
            if ($line -match "(?i)\bexception\b") { $state.ExceptionLines++ }
        }
        $state.Status = "summary-captured"
        return $state
    } catch {
        return [ordered]@{
            Status = "failed"
            Bytes = $null
            LastWriteTimeUtc = $null
            LinesRead = 0
            ErrorLines = 0
            WarningLines = 0
            TimeoutLines = 0
            ExceptionLines = 0
            ErrorType = $_.Exception.GetType().Name
        }
    }
}

function Get-VipmFailureDiagnostic {
    param(
        [string]$DebugLogPath,
        [string]$Signature = "library_add_package_no_progress_60s",
        [ValidateRange(1, 15)]
        [int]$TimeoutSeconds = 5,
        [scriptblock]$ProcessQuery
    )

    if (-not $ProcessQuery) {
        $ProcessQuery = {
            $allowedNames = @("LabVIEW", "VIPM", "vipm", "vipm-cli", "VIPMDesktop")
            foreach ($process in Get-Process -Name $allowedNames -ErrorAction SilentlyContinue | Select-Object -First 16) {
                $hasWindow = $false
                $windowState = "unavailable"
                $responding = $null
                try {
                    $hasWindow = [IntPtr]$process.MainWindowHandle -ne [IntPtr]::Zero
                    if (-not $hasWindow) {
                        $windowState = "no-main-window"
                    } elseif ($process.MainWindowTitle -match "(?i)\b(error|warning|retry|cancel|confirm|not responding|install|license|activation)\b") {
                        $windowState = "attention-or-dialog-suspected"
                    } else {
                        $windowState = "main-window-present"
                    }
                    $responding = [bool]$process.Responding
                } catch {
                    $windowState = "unavailable"
                }
                [pscustomobject]@{
                    ProcessName = [string]$process.ProcessName
                    ProcessId = [int]$process.Id
                    Responding = $responding
                    WindowState = $windowState
                }
            }
        }
    }

    $processCapture = Invoke-BoundedDiagnosticJob -ScriptBlock $ProcessQuery -TimeoutSeconds $TimeoutSeconds
    $allowedNames = @("LabVIEW", "VIPM", "vipm", "vipm-cli", "VIPMDesktop")
    $safeProcesses = @(
        foreach ($process in $processCapture.Result | Select-Object -First 16) {
            $name = [string]$process.ProcessName
            if ($name -notin $allowedNames) { continue }
            $windowState = [string]$process.WindowState
            if ($windowState -notin @("unavailable", "no-main-window", "attention-or-dialog-suspected", "main-window-present")) {
                $windowState = "unavailable"
            }
            [ordered]@{
                ProcessName = $name
                ProcessId = [int]$process.ProcessId
                Responding = if ($process.Responding -is [bool]) { $process.Responding } else { $null }
                WindowState = $windowState
            }
        }
    )

    return [ordered]@{
        SchemaVersion = 1
        Signature = $Signature
        CapturedAtUtc = [DateTime]::UtcNow.ToString("o")
        ProcessCapture = [ordered]@{
            Status = $processCapture.Status
            DurationMilliseconds = $processCapture.DurationMilliseconds
            ErrorType = $processCapture.ErrorType
            Processes = $safeProcesses
        }
        VipmDebugLog = if ($DebugLogPath) { Get-VipmDebugLogState -Path $DebugLogPath } else { [ordered]@{ Status = "not-applicable" } }
    }
}

function Invoke-PackageVipmInstall {
    param(
        [string[]]$Arguments,
        [string]$WorkingDirectory,
        [string]$DebugLogPath,
        [System.Collections.IDictionary]$InstallationReport,
        [switch]$RetryLibraryAddStall
    )
    $captureDebug = [bool]$RetryLibraryAddStall
    $previousDebug = [Environment]::GetEnvironmentVariable("VIPM_DEBUG", "Process")
    $previousLogPath = [Environment]::GetEnvironmentVariable("VIPM_DEBUG_LOG", "Process")
    try {
        if ($captureDebug) {
            try {
                [System.IO.File]::WriteAllText(
                    $DebugLogPath,
                    "VIPM_DEBUG=1 initial package install attempt$([Environment]::NewLine)",
                    [System.Text.UTF8Encoding]::new($false)
                )
            } catch {
                throw "VIPM package install was not attempted because trace file '$DebugLogPath' could not be created: $($_.Exception.Message)"
            }
            $env:VIPM_DEBUG = "1"
            $env:VIPM_DEBUG_LOG = $DebugLogPath
        }

        try {
            Invoke-Vipm -Arguments $Arguments -WorkingDirectory $WorkingDirectory -ShowProgress
            if (-not $captureDebug) { return }
            try {
                Remove-Item -LiteralPath $DebugLogPath -Force -ErrorAction Stop
                return
            } catch {
                Write-PipelineLog -Message "VIPM install succeeded, but its debug trace '$DebugLogPath' could not be removed and will be retained." -Level WARN
                return $DebugLogPath
            }
        } catch {
            $initialFailure = $_.Exception.Message
            if (-not $RetryLibraryAddStall -or $initialFailure -notmatch '(?is)library_add_package.*made no progress for 60s') {
                throw
            }
        }

        if ($InstallationReport) {
            $InstallationReport["FailureDiagnostics"] = Get-VipmFailureDiagnostic -DebugLogPath $DebugLogPath
        }
        Write-PipelineLog -Message "VIPM install stalled in library_add_package for 60s; retrying once with VIPM_DEBUG=1. Trace: '$DebugLogPath'." -Level WARN
        try {
            [System.IO.File]::AppendAllText(
                $DebugLogPath,
                "Retry with VIPM_DEBUG=1 after initial failure: $initialFailure$([Environment]::NewLine)",
                [System.Text.UTF8Encoding]::new($false)
            )
        } catch {
            throw "Initial VIPM install failure: $initialFailure The diagnostic retry was not attempted because trace file '$DebugLogPath' could not be appended: $($_.Exception.Message)"
        }
        try {
            Invoke-Vipm -Arguments $Arguments -WorkingDirectory $WorkingDirectory -ShowProgress
        } catch {
            throw "Initial VIPM install failure: $initialFailure Retry with VIPM_DEBUG=1 failed: $($_.Exception.Message) VIPM_DEBUG trace: '$DebugLogPath'."
        }
        return $DebugLogPath
    } finally {
        if ($captureDebug) {
            [Environment]::SetEnvironmentVariable("VIPM_DEBUG", $previousDebug, "Process")
            [Environment]::SetEnvironmentVariable("VIPM_DEBUG_LOG", $previousLogPath, "Process")
        }
    }
}

function Invoke-PackageBuild {
    <#
    Builds each planned VIPB target with `vipm build <target>`, installs the
    produced .vip so later targets and the project build resolve it, and checks
    VIPM really installed it, because `vipm install` can report a package as
    absent when its custom action VIs fail.

    The .vip and its build report (packages/<name>.json) are archived as soon
    as the build succeeds. The report's Status then moves from "built" to
    "installation-failed", "verification-failed" or "verified-installed", so a
    failed run keeps the binary with truthful evidence. Only verified-installed
    reports are returned.
    #>
    param(
        [object[]]$Plan,
        [string]$ProjectDir,
        [string]$OutputDir,
        [string]$QueryDir,
        [string]$LabviewVersion,
        [string]$LabviewBitness,
        [bool]$AllowPackageDrift,
        [switch]$RetryVipmLibraryAddStall
    )
    $archiveDir = [System.IO.Path]::Combine($OutputDir, "packages")
    $null = New-Item -ItemType Directory -Path $archiveDir -Force
    # VIPB targets ignore vipm.toml and .vipm/config.toml and auto-detect LabVIEW from
    # the VIPB's Package_LabVIEW_Version, so the version must be passed explicitly.
    $lvArgs = @("--labview-version", $LabviewVersion, "--labview-bitness", $LabviewBitness)
    if ($Plan.Count -gt 0) {
        Write-PipelineLog -Message "Installing manifest dependencies before building VI packages..." -Level INFO
        try {
            Invoke-Vipm -Arguments (@("install", "-y") + $lvArgs) -WorkingDirectory $ProjectDir -ShowProgress
        } catch {
            if ($_.Exception.Message -match '(?is)package_set_install.*made no progress for \d+(?:\.\d+)?s') {
                # A hung LabVIEW/VIPM Desktop cannot be recovered by retrying here (see
                # issue #244): capture bounded, redacted evidence for this exact failure
                # and still fail fast, rather than masking it with a retry.
                $diagnosticPath = [System.IO.Path]::Combine($archiveDir, "manifest-dependency-failure-diagnostics.json")
                try {
                    $diagnostic = Get-VipmFailureDiagnostic -Signature "package_set_install_no_progress"
                    $diagnostic | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $diagnosticPath -Encoding utf8
                    Write-PipelineLog -Message "Captured bounded failure diagnostics for the manifest dependency install stall: '$diagnosticPath'." -Level WARN
                } catch {
                    Write-PipelineLog -Message "Manifest dependency install stalled, but bounded failure diagnostics could not be captured: $($_.Exception.Message)" -Level WARN
                }
            }
            throw
        }
        $dependencies = ConvertFrom-Json -InputObject (Invoke-Vipm -Arguments (@("list", "--installed") + $lvArgs) -WorkingDirectory $ProjectDir -Json)
        if ($null -eq $dependencies.dependencies) {
            throw "VIPM returned no dependency inventory after installing manifest dependencies."
        }
        $missing = @($dependencies.dependencies | Where-Object { $_.status -ne "installed" })
        if ($missing.Count -gt 0) {
            throw "Manifest dependencies are not installed after vipm install: $(($missing | ForEach-Object name) -join ', ')."
        }
    }
    Write-RunningLabviewWarning
    $reports = [System.Collections.Generic.List[object]]::new()
    foreach ($step in $Plan) {
        Write-VipbOutputFolder -VipbPath $step.VipbPath -OutputDir $step.OutputDir
        # Targets run in dependency order, so vipm must not rebuild depends-on targets again.
        $buildArgs = @("build", $step.Target, "--no-deps") + $lvArgs
        if ($AllowPackageDrift) { $buildArgs += "--allow-package-drift" }
        Write-PipelineLog -Message "Building package target '$($step.Target)' ($($step.VipbRelativePath), saved for LabVIEW '$($step.VipbLabviewVersion)') with LabVIEW $LabviewVersion ($LabviewBitness-bit)..." -Level INFO
        Invoke-VipmBuild -Arguments $buildArgs -WorkingDirectory $ProjectDir
        $vipPath = Find-BuiltVipPackage -OutputDir $step.OutputDir -PackageName $step.PackageName
        $spec = Read-VipPackageSpec -VipPath $vipPath
        Copy-Item -LiteralPath $vipPath -Destination ([System.IO.Path]::Combine($archiveDir, [System.IO.Path]::GetFileName($vipPath))) -Force
        $report = [PSCustomObject]@{
            Target = $step.Target
            VipbPath = $step.VipbRelativePath
            Name = $spec.Name
            Version = $spec.Version
            Requires = $spec.Requires
            VipFile = [System.IO.Path]::GetFileName($vipPath)
            Sha256 = (Get-FileHash -LiteralPath $vipPath -Algorithm SHA256).Hash.ToLowerInvariant()
            LabviewVersion = $LabviewVersion
            LabviewBitness = $LabviewBitness
            Status = "built"
            Installation = [ordered]@{ InstalledVersion = $null; Error = $null; DebugTrace = $null }
        }
        Write-PackageBuildReport -Report $report -ArchiveDir $archiveDir
        Write-PipelineLog -Message "Built $($spec.Name) $($spec.Version) ($($report.VipFile)); installing into LabVIEW $LabviewVersion ($LabviewBitness-bit)..." -Level INFO
        try {
            $debugLogName = [System.IO.Path]::GetFileNameWithoutExtension($vipPath) + "-vipm-debug"
            $debugLogPath = [System.IO.Path]::Combine($archiveDir, "$debugLogName.log")
            if (Test-Path -LiteralPath $debugLogPath) {
                $timestamp = (Get-Date).ToString("yyyyMMdd-HHmmss-fff")
                $debugLogPath = [System.IO.Path]::Combine($archiveDir, "$debugLogName-$timestamp.log")
            }
            $retryTracePath = Invoke-PackageVipmInstall -Arguments (@("install", $vipPath, "-y") + $lvArgs) `
                -WorkingDirectory $ProjectDir -DebugLogPath $debugLogPath -InstallationReport $report.Installation `
                -RetryLibraryAddStall:$RetryVipmLibraryAddStall
            if ($retryTracePath) { $report.Installation.DebugTrace = [System.IO.Path]::GetFileName($retryTracePath) }
        } catch {
            $report.Status = "installation-failed"
            $report.Installation.Error = $_.Exception.Message
            if (Test-Path -LiteralPath $debugLogPath -PathType Leaf) {
                $report.Installation.DebugTrace = [System.IO.Path]::GetFileName($debugLogPath)
            }
            Write-PackageBuildReport -Report $report -ArchiveDir $archiveDir
            throw "Built package $($spec.Name) $($spec.Version) from '$($step.VipbRelativePath)' could not be installed: $($_.Exception.Message) The built package and its report are kept in '$archiveDir'. A possible cause is a failing Pre/Post-Install custom action VI in the VIPB, because VIPM drops a package whose custom action fails."
        }
        $verificationError = $null
        try {
            $installed = ConvertFrom-Json -InputObject (Invoke-Vipm -Arguments @(
                    "list", "--installed", "--labview-version", $LabviewVersion, "--labview-bitness", $LabviewBitness
                ) -Json -WorkingDirectory $QueryDir)
            if ($null -eq $installed -or $null -eq $installed.PSObject.Properties["dependencies"]) {
                $verificationError = "VIPM returned no installed-package inventory."
            } else {
                $installedVersion = @($installed.dependencies | Where-Object { $_ -and $_.name -eq $spec.Name } | ForEach-Object { [string]$_.installed_version }) | Select-Object -First 1
                $report.Installation.InstalledVersion = $installedVersion
                if ($installedVersion -ne $spec.Version) {
                    $verificationError = "VIPM reports installed version $(if ($installedVersion) { $installedVersion } else { 'none' })."
                }
            }
        } catch {
            $verificationError = "Installed-package inventory could not be read: $($_.Exception.Message)"
        }
        if ($verificationError) {
            $report.Status = "verification-failed"
            $report.Installation.Error = $verificationError
            Write-PackageBuildReport -Report $report -ArchiveDir $archiveDir
            throw "Built package $($spec.Name) $($spec.Version) is not verified as installed after vipm install: $verificationError The built package and its report are kept in '$archiveDir'."
        }
        $report.Status = "verified-installed"
        Write-PackageBuildReport -Report $report -ArchiveDir $archiveDir
        Write-PipelineLog -Message "Built and installed $($spec.Name) $($spec.Version) ($($report.VipFile))." -Level SUCCESS
        $reports.Add($report)
    }
    return $reports.ToArray()
}

function Add-PackageBuildComponent {
    <#
    Records each built package as a pkg:vipm component (the purl vipm sbom uses
    for the same package in consuming projects, so Dependency-Track correlates
    them) with the .vip hash, the VIPB that produced it and the source revision.
    #>
    param([System.Collections.IDictionary]$Sbom, [object[]]$Reports, [string]$RepoUrl, [string]$Revision)
    $rootRef = [string]$Sbom.metadata.component["bom-ref"]
    if (-not $rootRef) { throw "Project SBOM metadata.component is missing bom-ref." }
    $components = [System.Collections.Generic.List[object]]::new()
    $byPurl = @{}
    $byName = @{}
    foreach ($component in @($Sbom.components)) {
        if (-not $component) { continue }
        $components.Add($component)
        if ($component.purl) { $byPurl[[System.Uri]::UnescapeDataString([string]$component.purl)] = $component }
        if ($component.name) { $byName[([string]$component.name).ToLowerInvariant()] = $component }
    }
    $graph = @{}
    foreach ($node in @($Sbom.dependencies)) {
        if (-not $node) { continue }
        $graph[[string]$node.ref] = [System.Collections.Generic.HashSet[string]]::new([string[]]@($node.dependsOn | Where-Object { $_ }), [System.StringComparer]::Ordinal)
    }
    if (-not $graph.ContainsKey($rootRef)) { $graph[$rootRef] = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::Ordinal) }
    foreach ($report in $Reports) {
        $purl = "pkg:vipm/$($report.Name)@$($report.Version)"
        $component = $byPurl[$purl]
        if ($null -eq $component) {
            $component = [ordered]@{ "bom-ref" = $purl; type = "library"; name = $report.Name; version = $report.Version; purl = $purl }
            $components.Add($component)
            $byPurl[$purl] = $component
        }
        $component["group"] = "vipm"
        $component["hashes"] = @(@{ alg = "SHA-256"; content = $report.Sha256 })
        $component["properties"] = @($component["properties"] | Where-Object { $_ -and -not ([string]$_.name).StartsWith("build:") }) + @(
            @{ name = "build:target"; value = $report.Target },
            @{ name = "build:vipb"; value = $report.VipbPath },
            @{ name = "build:vip"; value = $report.VipFile },
            @{ name = "build:revision"; value = $Revision }
        )
        $component["externalReferences"] = @(@{ type = "vcs"; url = $RepoUrl; comment = "Built from $($report.VipbPath) at revision $Revision" })
        $byName[$report.Name.ToLowerInvariant()] = $component
        [void]$graph[$rootRef].Add([string]$component["bom-ref"])
    }
    foreach ($report in $Reports) {
        $ref = [string]$byPurl["pkg:vipm/$($report.Name)@$($report.Version)"]["bom-ref"]
        if (-not $graph.ContainsKey($ref)) { $graph[$ref] = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::Ordinal) }
        foreach ($required in @($report.Requires)) {
            $dependency = $byName[([string]$required).ToLowerInvariant()]
            if ($null -ne $dependency) { [void]$graph[$ref].Add([string]$dependency["bom-ref"]) }
        }
    }
    $Sbom.components = $components.ToArray()
    $Sbom.dependencies = @(foreach ($ref in $graph.Keys) { @{ ref = $ref; dependsOn = @($graph[$ref] | Sort-Object) } })
}

function Resolve-DependencyTrackPolicy {
    param([string]$Mode, [string]$ApiKey, [string]$SecurityGate = "1")
    $normalizedMode = ([string]$Mode).Trim().ToLowerInvariant()
    if ($normalizedMode -notin @("full", "hosted-grype")) {
        throw "DEPENDENCY_TRACK_MODE must be 'full' or 'hosted-grype'."
    }
    if ($normalizedMode -eq "full") {
        if ([string]::IsNullOrWhiteSpace($ApiKey)) {
            throw "Full Dependency-Track reporting requires DEPENDENCY_TRACK_API_KEY."
        }
        if ($SecurityGate -eq "0") {
            throw "Full Dependency-Track reporting cannot disable DEPENDENCY_TRACK_SECURITY_GATE."
        }
    }
    return $normalizedMode
}

function Test-DependencyTrackReadiness {
    param([string]$Mode, [string]$BaseUrl, [string]$ApiKey, [string]$SecurityGate = "1")
    $resolvedMode = Resolve-DependencyTrackPolicy -Mode $Mode -ApiKey $ApiKey -SecurityGate $SecurityGate
    if ($resolvedMode -eq "hosted-grype") { return $resolvedMode }
    $baseUri = $null
    if (-not [uri]::TryCreate($BaseUrl, [System.UriKind]::Absolute, [ref]$baseUri) -or $baseUri.Scheme -ne "https") {
        throw "Full Dependency-Track reporting requires an absolute HTTPS DEPENDENCY_TRACK_URL."
    }
    $openApiUrl = $BaseUrl.TrimEnd('/') + "/api/openapi.json"
    try {
        $headers = @{ "X-Api-Key" = $ApiKey; Accept = "application/json" }
        $openApi = Invoke-RestMethod -Method Get -Uri $openApiUrl -Headers $headers -TimeoutSec 10 -ErrorAction Stop
    } catch {
        $statusCode = 0
        if ($_.Exception.Response -and $_.Exception.Response.StatusCode) {
            $statusCode = [int]$_.Exception.Response.StatusCode
        }
        $detail = if ($statusCode) { "HTTP $statusCode" }
            elseif ($_.Exception.InnerException -and $_.Exception.InnerException.Status) { [string]$_.Exception.InnerException.Status }
            else { $_.Exception.GetType().Name }
        throw "Full Dependency-Track reporting preflight failed ($detail): the configured backend API is unavailable."
    }
    $paths = if ($openApi.paths -is [System.Collections.IDictionary]) { @($openApi.paths.Keys) } else { @($openApi.paths.PSObject.Properties.Name) }
    if ($null -eq $openApi.paths -or $paths.Count -eq 0) {
        throw "Full Dependency-Track reporting preflight failed: the backend OpenAPI document has no API paths."
    }
    return $resolvedMode
}

function New-PipelineRunRecord {
    <#
    Starts the per-project pipeline-run.json record. Test-SbomArtifact.ps1 reads it
    to tell an explicitly skipped stage from missing output, and a build or
    integrity failure from a vulnerability-policy or reporting failure.
    #>
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute("PSUseShouldProcessForStateChangingFunctions", "", Justification = "Builds an in-memory record.")]
    param(
        [string]$Project,
        [string]$Pipeline,
        [ValidateSet("full", "hosted-grype")][string]$ReportingMode = "full"
    )
    return [ordered]@{
        schemaVersion   = 1
        project         = $Project
        pipeline        = $Pipeline
        reportingMode   = $ReportingMode
        status          = "running"
        failureCategory = $null
        failure         = $null
        source          = $null
        patches         = @()
        stages          = [ordered]@{}
    }
}

function Set-PipelineStage {
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute("PSUseShouldProcessForStateChangingFunctions", "", Justification = "Updates an in-memory record.")]
    param(
        [System.Collections.IDictionary]$Record,
        [string]$Name,
        [ValidateSet("running", "completed", "skipped", "failed")][string]$Status,
        [string]$Reason,
        [ValidateSet("", "passed", "failed", "not-gated", "not-enforced", "not-evaluated")][string]$Gate
    )
    $stage = if ($Record.stages.Contains($Name)) { $Record.stages[$Name] } else { [ordered]@{} }
    $stage["status"] = $Status
    if ($Reason) { $stage["reason"] = $Reason }
    if ($Gate) { $stage["gate"] = $Gate }
    $Record.stages[$Name] = $stage
}

function Set-PipelineFindingsExportFailure {
    # The findings export is best effort; record why the report is absent instead of leaving it missing.
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute("PSUseShouldProcessForStateChangingFunctions", "", Justification = "Updates an in-memory record.")]
    param([System.Collections.IDictionary]$Record, [string]$Message)
    if ($null -eq $Record) { return }
    if (-not $Record.stages.Contains("dependencyTrack")) { $Record.stages["dependencyTrack"] = [ordered]@{ status = "running" } }
    $Record.stages["dependencyTrack"]["findingsExportError"] = $Message
}

function Complete-PipelineRunRecord {
    # A failed security gate is a policy failure; a failure while publishing to
    # Dependency-Track is a reporting failure; anything else is a build failure.
    param([System.Collections.IDictionary]$Record, [string]$Failure)
    if (-not $Failure) {
        $Record.status = "succeeded"
        return
    }
    $Record.status = "failed"
    $Record.failure = $Failure
    $running = @($Record.stages.Keys | Where-Object { $Record.stages[$_].status -eq "running" })
    foreach ($name in $running) { $Record.stages[$name].status = "failed" }
    $Record.failureCategory = if (@($Record.stages.Values | Where-Object { $_.gate -eq "failed" }).Count -gt 0) { "policy" }
        elseif ($running -contains "dependencyTrack") { "reporting" }
        else { "build" }
}

function Write-PipelineRunRecord {
    param([System.Collections.IDictionary]$Record, [string]$OutputDir)
    $null = New-Item -ItemType Directory -Path $OutputDir -Force
    $path = [System.IO.Path]::Combine($OutputDir, "pipeline-run.json")
    [System.IO.File]::WriteAllText($path, (ConvertTo-Json -InputObject $Record -Depth 10) + "`n", [System.Text.UTF8Encoding]::new($false))
}
