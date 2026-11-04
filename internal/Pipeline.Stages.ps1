# Internal, dot-sourced stage library: loading it defines functions only.
# The caller supplies Write-PipelineLog and the helpers from Build-Sbom.ps1.
# Stage exceptions propagate to Run-Pipeline.ps1's existing per-project handler.

function Invoke-CheckoutPathRemoval {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)
    Remove-Item -LiteralPath $LiteralPath -Recurse -Force -ErrorAction Stop
}

function Invoke-CheckoutFileLockLookup {
    param(
        [Parameter(Mandatory = $true)][string]$LiteralPath,
        [ValidateRange(1, 15)][int]$TimeoutSeconds = 5
    )

    if (-not $IsWindows) {
        return [pscustomobject]@{
            Status = "unsupported-platform"
            DurationMilliseconds = 0
            ErrorType = $null
            Processes = @()
        }
    }

    $restartManagerSource = @'
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;

namespace SandboxSbom.Reporting.Diagnostics
{
    [StructLayout(LayoutKind.Sequential)]
    public struct RmSystemTime
    {
        public ushort Year, Month, DayOfWeek, Day, Hour, Minute, Second, Milliseconds;
    }

    [StructLayout(LayoutKind.Sequential)]
    public struct RmUniqueProcess
    {
        public int ProcessId;
        public RmSystemTime ProcessStartTime;
    }

    public enum RmApplicationType
    {
        Unknown = 0, MainWindow = 1, OtherWindow = 2, Service = 3,
        Explorer = 4, Console = 5, Critical = 1000
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct RmProcessInfo
    {
        public RmUniqueProcess Process;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)]
        public string ApplicationName;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 64)]
        public string ServiceShortName;
        public RmApplicationType ApplicationType;
        public uint ApplicationStatus;
        public uint TerminalSessionId;
        [MarshalAs(UnmanagedType.Bool)]
        public bool Restartable;
    }

    public static class RestartManagerLookup
    {
        private const int ErrorMoreData = 234;
        private const uint MaximumProcesses = 32;

        [DllImport("rstrtmgr.dll", CharSet = CharSet.Unicode)]
        private static extern int RmStartSession(out uint sessionHandle, int flags, StringBuilder sessionKey);

        [DllImport("rstrtmgr.dll", CharSet = CharSet.Unicode)]
        private static extern int RmRegisterResources(uint sessionHandle, uint fileCount, string[] files,
            uint applicationCount, RmUniqueProcess[] applications, uint serviceCount, string[] services);

        [DllImport("rstrtmgr.dll")]
        private static extern int RmGetList(uint sessionHandle, out uint processInfoNeeded,
            ref uint processInfoCount, [In, Out] RmProcessInfo[] affectedApplications, ref uint rebootReasons);

        [DllImport("rstrtmgr.dll")]
        private static extern int RmEndSession(uint sessionHandle);

        public static int[] GetProcessIds(string path)
        {
            uint session;
            var key = new StringBuilder(33);
            int result = RmStartSession(out session, 0, key);
            if (result != 0) throw new Win32Exception(result);

            try
            {
                result = RmRegisterResources(session, 1, new[] { path }, 0, null, 0, null);
                if (result != 0) throw new Win32Exception(result);

                uint needed, count = 0, rebootReasons = 0;
                result = RmGetList(session, out needed, ref count, null, ref rebootReasons);
                if (result == 0) return Array.Empty<int>();
                if (result != ErrorMoreData) throw new Win32Exception(result);

                count = Math.Min(needed, MaximumProcesses);
                var processes = new RmProcessInfo[count];
                result = RmGetList(session, out needed, ref count, processes, ref rebootReasons);
                if (result != 0 && result != ErrorMoreData) throw new Win32Exception(result);

                var ids = new List<int>();
                for (int index = 0; index < count; index++)
                {
                    if (processes[index].Process.ProcessId > 0)
                        ids.Add(processes[index].Process.ProcessId);
                }
                return ids.ToArray();
            }
            finally
            {
                RmEndSession(session);
            }
        }
    }
}
'@

    $lookup = Invoke-BoundedDiagnosticJob -TimeoutSeconds $TimeoutSeconds -ArgumentList @($LiteralPath, $restartManagerSource) -ScriptBlock {
        param($ResourcePath, $Source)
        if (-not ("SandboxSbom.Reporting.Diagnostics.RestartManagerLookup" -as [type])) {
            Add-Type -TypeDefinition $Source -ErrorAction Stop
        }
        $processIds = [SandboxSbom.Reporting.Diagnostics.RestartManagerLookup]::GetProcessIds($ResourcePath)
        foreach ($processId in $processIds | Select-Object -First 32) {
            $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
            if (-not $process) {
                [pscustomobject]@{
                    ProcessId = [int]$processId
                    ProcessName = "unavailable"
                    Responding = $null
                    WindowState = "unavailable"
                }
                continue
            }

            $windowState = "no-main-window"
            $responding = $null
            try {
                if ([IntPtr]$process.MainWindowHandle -ne [IntPtr]::Zero) {
                    $title = [string]$process.MainWindowTitle
                    $windowState = if ($title -match "(?i)\b(error|warning|retry|cancel|confirm|not responding|install|license|activation)\b") {
                        "attention-or-dialog-suspected"
                    } else {
                        "main-window-present"
                    }
                }
                $responding = [bool]$process.Responding
            } catch {
                $windowState = "unavailable"
            }
            [pscustomobject]@{
                ProcessId = [int]$process.Id
                ProcessName = [string]$process.ProcessName
                Responding = $responding
                WindowState = $windowState
            }
        }
    }

    return [pscustomobject]@{
        Status = $lookup.Status
        DurationMilliseconds = $lookup.DurationMilliseconds
        ErrorType = $lookup.ErrorType
        Processes = @($lookup.Result | Select-Object -First 32)
    }
}

function Get-CheckoutFailureDiagnostic {
    param(
        [Parameter(Mandatory = $true)][string]$LiteralPath,
        [ValidateRange(1, 15)][int]$TimeoutSeconds = 5
    )

    $resource = [System.IO.FileInfo]::new($LiteralPath)
    $resourceType = "file"
    $length = $null
    $lastWriteTimeUtc = $null
    $attributes = $null
    try {
        if ([System.IO.Directory]::Exists($LiteralPath)) {
            $resourceType = "directory"
            $attributes = [string][System.IO.File]::GetAttributes($LiteralPath)
        } elseif ([System.IO.File]::Exists($LiteralPath)) {
            $resource.Refresh()
            $length = [long]$resource.Length
            $lastWriteTimeUtc = $resource.LastWriteTimeUtc.ToString("o")
            $attributes = [string]$resource.Attributes
        } else {
            $resourceType = "missing"
        }
    } catch {
        $attributes = "unavailable"
    }

    $lookup = Invoke-CheckoutFileLockLookup -LiteralPath $LiteralPath -TimeoutSeconds $TimeoutSeconds
    $owners = @(
        foreach ($process in $lookup.Processes | Select-Object -First 32) {
            $windowState = [string]$process.WindowState
            if ($windowState -notin @("unavailable", "no-main-window", "attention-or-dialog-suspected", "main-window-present")) {
                $windowState = "unavailable"
            }
            [ordered]@{
                ProcessId = [int]$process.ProcessId
                ProcessName = [string]$process.ProcessName
                Responding = if ($process.Responding -is [bool]) { $process.Responding } else { $null }
                WindowState = $windowState
            }
        }
    )

    return [ordered]@{
        SchemaVersion = 1
        CapturedAtUtc = [DateTime]::UtcNow.ToString("o")
        ResourceName = [System.IO.Path]::GetFileName($LiteralPath)
        ResourceType = $resourceType
        LengthBytes = $length
        LastWriteTimeUtc = $lastWriteTimeUtc
        Attributes = $attributes
        LockOwnerLookup = [ordered]@{
            Status = $lookup.Status
            DurationMilliseconds = $lookup.DurationMilliseconds
            ErrorType = $lookup.ErrorType
            Processes = $owners
        }
    }
}

function Invoke-PipelineCheckoutStage {
    # Inputs: resolved VCS/auth/sparse settings and checkout directory.
    # Outputs: native command output and ResolvedRevision (SVN sparse checkout pins it).
    # Failure: throws on wipe/checkout/update errors; patches and stage records remain caller-owned.
    param(
        [string]$ProjectWorkingDir, [string]$VcsType, [string]$RepoUrl,
        [string]$Revision, [string]$Username, [string]$Secret,
        [array]$SparsePaths, [ref]$ResolvedRevision
    )

    if ([System.IO.Directory]::Exists($ProjectWorkingDir)) {
        Write-PipelineLog -Message "Wiping existing working directory '$ProjectWorkingDir'..." -Level TRACE
        # -Force enumeration includes hidden .svn/.git; a stale one turns the checkout below into an update of the old working copy.
        $wipeErrors = [System.Collections.Generic.List[System.Management.Automation.ErrorRecord]]::new()
        foreach ($item in Get-ChildItem -LiteralPath $ProjectWorkingDir -Force) {
            try {
                Invoke-CheckoutPathRemoval -LiteralPath $item.FullName
            } catch {
                $wipeErrors.Add($_)
            }
        }
        if ($wipeErrors.Count -gt 0) {
            $firstError = $wipeErrors[0]
            $failedPath = if ($firstError.TargetObject -is [System.IO.FileSystemInfo]) {
                $firstError.TargetObject.FullName
            } else {
                [string]$firstError.TargetObject
            }
            if (-not $failedPath) { $failedPath = $ProjectWorkingDir }
            $failureDiagnostics = Get-CheckoutFailureDiagnostic -LiteralPath $failedPath
            $diagnosticsJson = ConvertTo-Json -InputObject $failureDiagnostics -Depth 6 -Compress
            throw "Could not wipe working directory '$ProjectWorkingDir' ($($wipeErrors.Count) removal error(s); first affected path '$failedPath'): $($firstError.Exception.Message) The directory may be partially cleaned. Bounded failure diagnostics: $diagnosticsJson Close programs holding files there (for example a LabVIEW instance that loaded the sources), remove the specific blocking file if needed, and rerun."
        }
    } else {
        $null = New-Item -ItemType Directory -Force -Path $ProjectWorkingDir
    }

    if ($VcsType -eq 'git') {
        $cloneUrl = $RepoUrl
        if (-not [string]::IsNullOrEmpty($Secret)) {
            if ($RepoUrl -match '^https://') { $cloneUrl = $RepoUrl -replace 'https://', "https://$Secret@" }
        }
        Write-PipelineLog -Message "Enabling Git longpaths..." -Level TRACE
        git config --global core.longpaths true
        Write-PipelineLog -Message "Executing Git Clone command for URL: $($RepoUrl)..." -Level INFO
        git clone "$cloneUrl" "$ProjectWorkingDir"
        if ($LASTEXITCODE -ne 0) { throw "Git clone failed with exit code $LASTEXITCODE" }

        if ($Revision -ne "HEAD" -and $Revision -ne "") {
            Write-PipelineLog -Message "Checking out targeted git revision: $Revision" -Level DEBUG
            git -C "$ProjectWorkingDir" checkout "$Revision"
            if ($LASTEXITCODE -ne 0) { throw "Git checkout failed for revision '$Revision'" }
        }
    }
    elseif ($VcsType -eq 'svn') {
        Write-PipelineLog -Message "Executing SVN Checkout command for URL: $($RepoUrl)..." -Level INFO
        $svnAuth = @("--non-interactive", "--trust-server-cert")
        if (-not [string]::IsNullOrEmpty($Username) -and -not [string]::IsNullOrEmpty($Secret)) {
            $svnAuth += @("--username", $Username, "--password", $Secret)
        }
        $depthArgs = if ($SparsePaths.Count -gt 0) { @("--depth", "empty") } else { @() }
        svn checkout "$RepoUrl" "$ProjectWorkingDir" --revision "$Revision" @depthArgs @svnAuth
        if ($LASTEXITCODE -ne 0) { throw "SVN checkout failed with exit code $LASTEXITCODE" }
        if ($SparsePaths.Count -gt 0) {
            # Pin every sparse update to the revision the root checkout resolved.
            $Revision = (svn info --show-item revision "$ProjectWorkingDir" @svnAuth | Out-String).Trim()
            if ($LASTEXITCODE -ne 0 -or -not $Revision) { throw "Could not resolve the checked-out SVN revision." }
            foreach ($step in (Get-SvnSparseUpdateStep -Paths $SparsePaths)) {
                Write-PipelineLog -Message "Sparse checkout: $($step.Path) (depth $($step.Depth)) at r$Revision" -Level INFO
                svn update --set-depth $step.Depth --revision $Revision --quiet ([System.IO.Path]::Combine($ProjectWorkingDir, $step.Path)) @svnAuth
                if ($LASTEXITCODE -ne 0) { throw "SVN sparse update of '$($step.Path)' failed with exit code $LASTEXITCODE" }
            }
        }
    }
    $ResolvedRevision.Value = $Revision
}

function Invoke-PipelineSourceCheckoutStage {
    param(
        [Parameter(Mandatory = $true)][string]$ProjectWorkingDir,
        [Parameter(Mandatory = $true)][object[]]$SourceCheckouts
    )

    $projectRoot = [System.IO.Path]::GetFullPath($ProjectWorkingDir)
    if (-not (Test-Path -LiteralPath $projectRoot -PathType Container)) {
        throw "Project source-checkout root does not exist: '$projectRoot'."
    }
    if ((Get-Item -LiteralPath $projectRoot -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
        throw "Project source-checkout root must not be a link: '$projectRoot'."
    }
    $staged = [System.Collections.Generic.List[object]]::new()
    foreach ($source in $SourceCheckouts) {
        $repository = [string]$source.repository
        $revision = [string]$source.revision
        $checkoutPath = [string]$source.checkoutPath
        if ($repository -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$' -or
            [string]::IsNullOrWhiteSpace($revision) -or $revision.StartsWith('-') -or $revision -match '[*<>~^]' -or
            [string]::IsNullOrWhiteSpace($checkoutPath) -or [System.IO.Path]::IsPathRooted($checkoutPath) -or
            $checkoutPath -match '^[A-Za-z]:' -or $checkoutPath -match '(^|[\\/])\.\.([\\/]|$)' -or
            $checkoutPath -match '[<>:"|?*\x00-\x1F]') {
            throw "Source checkout '$repository' has an invalid repository, revision, or checkoutPath."
        }
        $targetPath = [System.IO.Path]::GetFullPath([System.IO.Path]::Combine($projectRoot, $checkoutPath))
        $relativeTarget = [System.IO.Path]::GetRelativePath($projectRoot, $targetPath)
        if ([System.IO.Path]::IsPathRooted($relativeTarget) -or $relativeTarget -eq '..' -or
            $relativeTarget.StartsWith('..' + [System.IO.Path]::DirectorySeparatorChar)) {
            throw "Source checkout path '$checkoutPath' escapes project '$projectRoot'."
        }
        $parentPath = [System.IO.Path]::GetDirectoryName($targetPath)
        while ($parentPath -and $parentPath -ne $projectRoot) {
            $parentItem = Get-Item -LiteralPath $parentPath -Force -ErrorAction SilentlyContinue
            if ($null -ne $parentItem -and ($parentItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
                throw "Source checkout path '$checkoutPath' traverses a link."
            }
            $parentPath = [System.IO.Path]::GetDirectoryName($parentPath)
        }
        if (Test-Path -LiteralPath $targetPath) {
            throw "Source checkout destination already exists; refusing to overwrite '$targetPath'."
        }
        $resolvedRevision = $revision
        Invoke-PipelineCheckoutStage -ProjectWorkingDir $targetPath -VcsType 'git' `
            -RepoUrl "https://github.com/$repository.git" -Revision $revision -SparsePaths @() `
            -ResolvedRevision ([ref]$resolvedRevision) | Out-Null
        $resolvedCommit = (git -C "$targetPath" rev-parse --verify 'HEAD^{commit}' | Out-String).Trim()
        if ($LASTEXITCODE -ne 0 -or $resolvedCommit -notmatch '^(?:[a-f0-9]{40}|[a-f0-9]{64})$') {
            throw "Could not resolve the checked-out Git commit for source checkout '$repository'."
        }
        $staged.Add([ordered]@{
            repository = $repository
            revision = $resolvedRevision
            resolvedRevision = $resolvedCommit
            checkoutPath = $checkoutPath
        })
    }
    return $staged.ToArray()
}

function Invoke-LvlibpReleaseDependencyStage {
    param(
        [Parameter(Mandatory = $true)][object[]]$Dependencies,
        [Parameter(Mandatory = $true)][string]$WorkspaceRoot
    )

    if ($Dependencies.Count -eq 0) { return @() }
    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
        throw 'GitHub CLI is required to fetch immutable LVLIBP release dependencies.'
    }

    $workspacePath = [System.IO.Path]::GetFullPath($WorkspaceRoot)
    if (-not (Test-Path -LiteralPath $workspacePath -PathType Container)) {
        throw "LVLIBP dependency workspace root does not exist: '$workspacePath'."
    }
    if ((Get-Item -LiteralPath $workspacePath -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
        throw "LVLIBP dependency workspace root must not be a link: '$workspacePath'."
    }
    $staged = [System.Collections.Generic.List[object]]::new()

    foreach ($dependency in $Dependencies) {
        $name = [string]$dependency.name
        $version = [string]$dependency.version
        $repository = [string]$dependency.repository
        $releaseTag = [string]$dependency.releaseTag
        $assetFileName = [string]$dependency.assetFileName
        $stagePath = [string]$dependency.stagePath
        if ([string]::IsNullOrWhiteSpace($name) -or [string]::IsNullOrWhiteSpace($version) -or
            $version -ieq 'latest' -or $version -match '[*<>~^]' -or [string]::IsNullOrWhiteSpace($repository) -or
            $repository -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$' -or
            [string]::IsNullOrWhiteSpace($releaseTag) -or $releaseTag -ieq 'latest' -or $releaseTag -match '[*<>~^]' -or
            $assetFileName -notmatch '^[A-Za-z0-9][A-Za-z0-9._+-]*\.lvlibp$' -or
            [string]::IsNullOrWhiteSpace($stagePath) -or [System.IO.Path]::IsPathRooted($stagePath) -or
            $stagePath -match '(^|[\\/])\.\.([\\/]|$)' -or $stagePath -match '[<>:"|?*\x00-\x1F]' -or
            $stagePath -notmatch '\.lvlibp$') {
            throw "LVLIBP release dependency '$name' has an invalid or floating pin/path."
        }

        $targetPath = [System.IO.Path]::GetFullPath([System.IO.Path]::Combine($workspacePath, $stagePath))
        $relativeTarget = [System.IO.Path]::GetRelativePath($workspacePath, $targetPath)
        if ([System.IO.Path]::IsPathRooted($relativeTarget) -or $relativeTarget -eq '..' -or
            $relativeTarget.StartsWith('..' + [System.IO.Path]::DirectorySeparatorChar)) {
            throw "LVLIBP stage path '$stagePath' escapes the checkout root."
        }

        $artifactDirectory = Join-Path ([System.IO.Path]::GetTempPath()) ("lba-release-dependency-" + [guid]::NewGuid().ToString('N'))
        $null = New-Item -ItemType Directory -Path $artifactDirectory
        try {
            $releaseEndpoint = 'repos/{0}/releases/tags/{1}' -f $repository, [uri]::EscapeDataString($releaseTag)
            $metadataJson = & gh api -- $releaseEndpoint
            if ($LASTEXITCODE -ne 0) { throw "GitHub release metadata query failed with exit code $LASTEXITCODE." }
            try {
                $metadata = ($metadataJson -join [Environment]::NewLine) | ConvertFrom-Json -AsHashtable -ErrorAction Stop
            } catch {
                throw "GitHub release metadata is invalid JSON: $($_.Exception.Message)"
            }
            if ($metadata.tag_name -cne $releaseTag -or $metadata.immutable -isnot [bool] -or
                -not $metadata.immutable -or $metadata.draft -isnot [bool] -or $metadata.draft) {
                throw "LVLIBP dependency '$name' is not a published immutable release at '$releaseTag'."
            }

            $verificationJson = & gh release verify --repo $repository --format json -- $releaseTag
            if ($LASTEXITCODE -ne 0) { throw "GitHub release attestation verification failed with exit code $LASTEXITCODE." }
            try {
                $verification = ($verificationJson -join [Environment]::NewLine) | ConvertFrom-Json -AsHashtable -ErrorAction Stop
            } catch {
                throw "GitHub release attestation output is invalid JSON: $($_.Exception.Message)"
            }
            $statement = $verification.verificationResult.statement
            $predicate = $statement.predicate
            if ($statement.predicateType -cne 'https://in-toto.io/attestation/release/v0.2' -or
                $predicate.repository -ine $repository -or $predicate.tag -cne $releaseTag -or
                [string]::IsNullOrWhiteSpace([string]$predicate.purl)) {
                throw "The attested release identity does not match '$repository@$releaseTag'."
            }

            $tagRefEndpoint = 'repos/{0}/git/ref/tags/{1}' -f $repository, [uri]::EscapeDataString($releaseTag)
            $tagRefJson = & gh api -- $tagRefEndpoint
            if ($LASTEXITCODE -ne 0) { throw "GitHub tag reference query failed with exit code $LASTEXITCODE." }
            try {
                $tagRef = ($tagRefJson -join [Environment]::NewLine) | ConvertFrom-Json -AsHashtable -ErrorAction Stop
            } catch {
                throw "GitHub tag reference is invalid JSON: $($_.Exception.Message)"
            }
            if ($tagRef.ref -cne "refs/tags/$releaseTag" -or
                $tagRef.object.sha -notmatch '^(?:[A-Fa-f0-9]{40}|[A-Fa-f0-9]{64})$' -or
                $tagRef.object.type -notin @('commit', 'tag')) {
                throw 'The pinned GitHub tag reference is missing or malformed.'
            }
            $attestedTagObjectSha = [string]$tagRef.object.sha
            $tagObject = $tagRef.object
            for ($tagDepth = 0; $tagObject.type -ceq 'tag'; $tagDepth++) {
                if ($tagDepth -ge 10) { throw 'The pinned GitHub tag has too many nested annotated tags.' }
                $tagObjectEndpoint = 'repos/{0}/git/tags/{1}' -f $repository, $tagObject.sha
                $tagObjectJson = & gh api -- $tagObjectEndpoint
                if ($LASTEXITCODE -ne 0) { throw "GitHub annotated tag query failed with exit code $LASTEXITCODE." }
                try {
                    $tagObjectRecord = ($tagObjectJson -join [Environment]::NewLine) | ConvertFrom-Json -AsHashtable -ErrorAction Stop
                } catch {
                    throw "GitHub annotated tag is invalid JSON: $($_.Exception.Message)"
                }
                if ($tagObjectRecord.sha -cne $tagObject.sha) { throw 'GitHub annotated tag response does not match the requested tag object.' }
                $tagObject = $tagObjectRecord.object
            }
            if ($tagObject.type -cne 'commit' -or
                $tagObject.sha -notmatch '^(?:[A-Fa-f0-9]{40}|[A-Fa-f0-9]{64})$') {
                throw 'The pinned GitHub tag does not resolve to a commit.'
            }
            $tagCommitSha = [string]$tagObject.sha

            $manifestDownload = @('release', 'download', $releaseTag, '--repo', $repository, '--dir', $artifactDirectory, '--pattern', 'release-manifest.json')
            $null = & gh @manifestDownload
            if ($LASTEXITCODE -ne 0) { throw "Could not download release-manifest.json for '$repository@$releaseTag'." }
            $manifestPath = Join-Path $artifactDirectory 'release-manifest.json'
            if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf) -or
                ((Get-Item -LiteralPath $manifestPath -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
                throw 'The downloaded release manifest is missing or is a link.'
            }
            try {
                $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding utf8 | ConvertFrom-Json -AsHashtable -ErrorAction Stop
            } catch {
                throw "Downloaded release manifest is invalid JSON: $($_.Exception.Message)"
            }
            if ($manifest.module.repository -ine $repository -or [string]::IsNullOrWhiteSpace([string]$manifest.module.id) -or
                $manifest.module.version -cne $version -or $manifest.source.repository -ine $repository -or
                $manifest.source.ref -cne "refs/tags/$releaseTag" -or $manifest.source.commit -notmatch '^(?:[A-Fa-f0-9]{40}|[A-Fa-f0-9]{64})$' -or
                $tagCommitSha -ine $manifest.source.commit -or
                $manifest.release.tag -cne $releaseTag -or $manifest.release.immutable -ne $true -or
                $manifest.validation.status -cne 'passed') {
                throw "Downloaded release manifest or the pinned tag target does not match LVLIBP dependency '$name@$version'."
            }

            $releaseAssets = @($manifest.artifacts.releaseAssets)
            $selectedAssets = @($releaseAssets | Where-Object {
                $_.id -ceq $name -and $_.version -ceq $version -and $_.kind -ceq 'lvlibp' -and
                $_.asset.fileName -ceq $assetFileName
            })
            if ($selectedAssets.Count -ne 1) {
                throw "Release manifest must identify exactly one LVLIBP asset '$assetFileName' for '$name@$version'."
            }
            $selectedSboms = @($manifest.artifacts.sboms | Where-Object {
                $_.subjectAssetId -ceq $name -and $_.subjectAssetVersion -ceq $version -and $_.subjectAssetKind -ceq 'lvlibp'
            })
            if ($selectedSboms.Count -ne 1 -or $selectedSboms[0].format -cne 'CycloneDX' -or $selectedSboms[0].specVersion -cne '1.5') {
                throw "Release manifest must identify exactly one CycloneDX 1.5 SBOM for '$name@$version'."
            }

            $sourceSubjects = @($statement.subject | Where-Object { $_.uri -ceq $predicate.purl })
            $tagHashAlgorithm = if ($attestedTagObjectSha.Length -eq 40) { 'sha1' } else { 'sha256' }
            if ($sourceSubjects.Count -ne 1 -or $sourceSubjects[0].digest[$tagHashAlgorithm] -ine $attestedTagObjectSha) {
                throw 'Attested source subject does not match the pinned Git tag object.'
            }

            $assetRecords = [System.Collections.Generic.List[object]]::new()
            $assetRecords.Add([ordered]@{
                fileName = 'release-manifest.json'
                sizeBytes = (Get-Item -LiteralPath $manifestPath).Length
                sha256 = (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
            })
            foreach ($entry in @($manifest.artifacts.releaseAssets) + @($manifest.artifacts.sboms)) {
                $assetRecords.Add($entry.asset)
            }
            if ($manifest.artifacts.attestation) { $assetRecords.Add($manifest.artifacts.attestation) }
            $assetNames = @($assetRecords | ForEach-Object { [string]$_.fileName })
            if (@($assetNames | Select-Object -Unique).Count -ne $assetNames.Count) {
                throw 'Release manifest contains duplicate asset filenames.'
            }
            foreach ($asset in $assetRecords) {
                if ($asset.fileName -notmatch '^[A-Za-z0-9][A-Za-z0-9._+-]*$' -or
                    $asset.sizeBytes -le 0 -or $asset.sha256 -notmatch '^[A-Fa-f0-9]{64}$') {
                    throw "Release manifest contains invalid metadata for asset '$($asset.fileName)'."
                }
                $releaseAssetMetadata = @($metadata.assets | Where-Object { $_.name -ceq $asset.fileName })
                if ($releaseAssetMetadata.Count -ne 1) { throw "Published release does not contain exactly one '$($asset.fileName)' asset." }
                $subjects = @($statement.subject | Where-Object { $_.name -ceq $asset.fileName })
                if ($subjects.Count -ne 1 -or $subjects[0].digest.sha256 -ine $asset.sha256) {
                    throw "Release asset '$($asset.fileName)' lacks a unique matching attested SHA-256."
                }
                if ($asset.fileName -ceq 'release-manifest.json') { continue }
                $assetDownload = @('release', 'download', $releaseTag, '--repo', $repository, '--dir', $artifactDirectory, '--pattern', $asset.fileName)
                $null = & gh @assetDownload
                if ($LASTEXITCODE -ne 0) { throw "Could not download release asset '$($asset.fileName)'." }
                $localAssetPath = Join-Path $artifactDirectory $asset.fileName
                if (-not (Test-Path -LiteralPath $localAssetPath -PathType Leaf) -or
                    ((Get-Item -LiteralPath $localAssetPath -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
                    throw "Downloaded release asset '$($asset.fileName)' is missing or is a link."
                }
                $localAsset = Get-Item -LiteralPath $localAssetPath
                $localHash = (Get-FileHash -LiteralPath $localAssetPath -Algorithm SHA256).Hash.ToLowerInvariant()
                if ($localAsset.Length -ne $asset.sizeBytes -or $localHash -ine $asset.sha256) {
                    throw "Downloaded release asset '$($asset.fileName)' does not match its signed manifest digest."
                }
            }

            $assetPath = Join-Path $artifactDirectory $assetFileName
            $sbomPath = Join-Path $artifactDirectory $selectedSboms[0].asset.fileName
            $sbom = Get-Content -LiteralPath $sbomPath -Raw -Encoding utf8 | ConvertFrom-Json -AsHashtable -ErrorAction Stop
            if ($sbom.bomFormat -cne 'CycloneDX' -or $sbom.specVersion -cne '1.5' -or
                $sbom.metadata.component.name -cne $name -or $sbom.metadata.component.version -cne $version) {
                throw "Release SBOM does not identify '$name@$version'."
            }

            $segments = $stagePath -split '[\\/]'
            $currentPath = $workspacePath
            for ($index = 0; $index -lt $segments.Count; $index++) {
                $currentPath = Join-Path $currentPath $segments[$index]
                if (Test-Path -LiteralPath $currentPath) {
                    $currentItem = Get-Item -LiteralPath $currentPath -Force
                    if ($currentItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
                        throw "LVLIBP stage path contains a linked path component: '$currentPath'."
                    }
                    if ($index -lt ($segments.Count - 1) -and -not $currentItem.PSIsContainer) {
                        throw "LVLIBP stage path parent is not a directory: '$currentPath'."
                    }
                } elseif ($index -lt ($segments.Count - 1)) {
                    $null = New-Item -ItemType Directory -Path $currentPath
                }
            }
            if (Test-Path -LiteralPath $targetPath -PathType Leaf) {
                $existingHash = (Get-FileHash -LiteralPath $targetPath -Algorithm SHA256).Hash.ToLowerInvariant()
                $releaseHash = (Get-FileHash -LiteralPath $assetPath -Algorithm SHA256).Hash.ToLowerInvariant()
                if ($existingHash -ine $releaseHash) { throw "Refusing to overwrite conflicting LVLIBP stage target '$targetPath'." }
            } else {
                $temporaryTarget = "$targetPath.$([guid]::NewGuid().ToString('N')).tmp"
                try {
                    [System.IO.File]::Copy($assetPath, $temporaryTarget, $false)
                    $copiedHash = (Get-FileHash -LiteralPath $temporaryTarget -Algorithm SHA256).Hash.ToLowerInvariant()
                    if ($copiedHash -ine $selectedAssets[0].asset.sha256) { throw 'Staged LVLIBP copy failed digest verification.' }
                    [System.IO.File]::Move($temporaryTarget, $targetPath)
                } finally {
                    if (Test-Path -LiteralPath $temporaryTarget) { Remove-Item -LiteralPath $temporaryTarget -Force }
                }
            }
            $staged.Add([ordered]@{
                name = $name
                version = $version
                repository = $repository
                releaseTag = $releaseTag
                assetFileName = $assetFileName
                stagePath = $stagePath
                sha256 = (Get-FileHash -LiteralPath $targetPath -Algorithm SHA256).Hash.ToLowerInvariant()
            })
            Write-PipelineLog -Message "Verified immutable LVLIBP dependency $name $version and staged its attested bytes at '$stagePath'." -Level SUCCESS
        } finally {
            Remove-Item -LiteralPath $artifactDirectory -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    return $staged.ToArray()
}

function Invoke-PipelineManifestStage {
    # Inputs: checkout root, configured TOML text, optional LabVIEW workspace override.
    # Output: initializes then replaces root vipm.toml with exact UTF-8 (no BOM) text.
    # Failure: propagates init/write errors, always restores the caller's location.
    param(
        [string]$ProjectWorkingDir, [string]$VipmTomlContent,
        [string]$LvOverrideVersion, [string]$LvOverrideBitness
    )
    $tomlPath = [System.IO.Path]::Combine($ProjectWorkingDir, "vipm.toml")
    Push-Location $ProjectWorkingDir
    try {
        if (Test-Path -LiteralPath $tomlPath) {
            Remove-Item -LiteralPath $tomlPath -Force
        }
        if ($LvOverrideVersion) {
            Write-VipmWorkspaceOverride -ProjectDir $ProjectWorkingDir -LabviewVersion $LvOverrideVersion -LabviewBitness $LvOverrideBitness
        }
        Write-PipelineLog -Message "Initializing VIPM manifest at repository root '$ProjectWorkingDir'..." -Level INFO
        Invoke-Vipm -Arguments @("init") -WorkingDirectory $ProjectWorkingDir
        if (-not (Test-Path -LiteralPath $tomlPath -PathType Leaf)) {
            throw "VIPM initialization completed without creating '$tomlPath'."
        }
    } finally {
        Pop-Location
    }
    [System.IO.File]::WriteAllText($tomlPath, $VipmTomlContent, [System.Text.UTF8Encoding]::new($false))
    Write-PipelineLog -Message "Deployed configured VIPM manifest to $tomlPath." -Level SUCCESS
}

function Invoke-PipelineSbomStage {
    # Inputs: manifest, output path, product identity and resolved LabVIEW settings.
    # Output: VIPM writes the raw CycloneDX 1.5 SBOM; enrichment stays caller-owned.
    # Failure: Invoke-Vipm errors propagate; ALLOW_PACKAGE_DRIFT retains its existing meaning.
    param(
        [string]$TomlPath, [string]$ProjectDir, [string]$ProjectRelativeName,
        [string]$ProductVersion, [string]$SbomPath, [string]$LvOverrideVersion,
        [string]$EffectiveLvVersion, [string]$EffectiveLvBitness
    )
    Write-PipelineLog -Message "Extracting VIPM SBOM for project '$ProjectRelativeName'..." -Level INFO
    $sbomArgs = @("sbom", $TomlPath, "--format", "cyclonedx", "--schema-version", "1.5")
    if (-not $LvOverrideVersion) {
        $sbomArgs += @("--labview-version", $EffectiveLvVersion, "--labview-bitness", $EffectiveLvBitness)
    }
    $sbomArgs += @("--product-name", $ProjectRelativeName, "--product-version", $ProductVersion, "--output", $SbomPath)
    if ($env:ALLOW_PACKAGE_DRIFT -eq "1") {
        Write-PipelineLog -Message "ALLOW_PACKAGE_DRIFT=1: package drift is reported as a warning and the SBOM records the installed versions." -Level WARN
        $sbomArgs += "--allow-package-drift"
    }
    Invoke-Vipm -Arguments $sbomArgs -WorkingDirectory $ProjectDir
}

function Invoke-PipelineScanStage {
    # Inputs: enriched SBOM, text report path and mutable run record.
    # Outputs: redirected Grype report and grype stage status; findings are not gated here.
    # Failure: any nonzero Grype exit throws, leaving the running stage for the caller to finalize.
    param([string]$SbomPath, [string]$GrypeReportPath, [System.Collections.IDictionary]$Record)
    Write-PipelineLog -Message "Scanning fully enriched SBOM with Grype..." -Level INFO
    Set-PipelineStage -Record $Record -Name "grype" -Status running
    & grype "sbom:$SbomPath" > "$GrypeReportPath" 2>&1
    if ($LASTEXITCODE -ne 0) { throw "Grype vulnerability scan failed with exit code $LASTEXITCODE." }
    # The native pipeline reports Grype findings; Dependency-Track metrics are its security gate.
    Set-PipelineStage -Record $Record -Name "grype" -Status completed -Gate not-gated
}

function Test-PipelinePolicyGate {
    # Full mode accepts only metrics that are bound to the exact imported BOM.
    param($Metrics, [string]$ProjectName, [long]$RequiredOccurrence)
    if ($null -eq $Metrics) { throw "Dependency-Track returned no project metrics; the security gate was not evaluated." }
    if ($RequiredOccurrence -le 0 -or $null -eq $Metrics.lastOccurrence -or [long]$Metrics.lastOccurrence -lt $RequiredOccurrence) {
        throw "Dependency-Track metrics are stale or not bound to the uploaded BOM for $ProjectName."
    }
    if ($null -eq $Metrics.critical -or $null -eq $Metrics.high) {
        throw "Dependency-Track metrics are missing Critical or High counts for $ProjectName."
    }
    $crit = [long]$Metrics.critical
    $high = [long]$Metrics.high
    Write-PipelineLog -Message "Risk Metrics Gate -> Critical: $crit | High: $high | Medium: $($Metrics.medium) | Low: $($Metrics.low)" -Level METRIC
    if ($crit -gt 0 -or $high -gt 0) {
        throw "SECURITY GATE FAILED: Found $crit Critical and $high High vulnerabilities in $ProjectName."
    }
    Write-PipelineLog -Message "Security Gate Passed!" -Level SUCCESS
    return "passed"
}

function Publish-DependencyTrack {
    # Inputs: endpoint/auth, project identity/metadata, SBOM and findings paths, mutable record.
    # Outputs: uploads/polls/patches/exports, then returns the policy gate result.
    # Failure: metadata/export failures remain nonfatal (export failure annotates Record);
    # upload/lookup/metrics/policy errors propagate. Caller owns dependencyTrack stage transitions.
    param(
        [string]$BaseUrl,
        [string]$ApiKey,
        [string]$ProjectName,
        [string]$ProjectVersion,
        [string]$SbomPath,
        [string]$ExportPath,
        [string]$Description,
        [array]$Tags,
        [System.Collections.IDictionary]$Record
    )

    Write-PipelineLog -Message "Pushing Enriched SBOM to upstream Dependency-Track instance..." -Level INFO
    $form = @{
        autoCreate     = "true"
        projectName    = $ProjectName
        projectVersion = $ProjectVersion
        bom            = Get-Item -LiteralPath $SbomPath
    }
    $dtHeaders = @{ "X-Api-Key" = $ApiKey; "Accept" = "application/json" }
    $encName = [uri]::EscapeDataString($ProjectName)
    $encVersion = [uri]::EscapeDataString($ProjectVersion)
    $lookupUri = "$BaseUrl/api/v1/project/lookup?name=$encName&version=$encVersion"
    $previousImport = 0L
    try {
        $previousProject = Invoke-RestMethod -Method Get -Uri $lookupUri -Headers $dtHeaders -ErrorAction Stop
        if ($previousProject.lastBomImport) { $previousImport = [long]$previousProject.lastBomImport }
    } catch {
        $statusCode = 0
        if ($_.Exception.Response -and $_.Exception.Response.StatusCode) {
            $statusCode = [int]$_.Exception.Response.StatusCode
        }
        if ($statusCode -ne 404) { throw "Dependency-Track project baseline lookup failed: $($_.Exception.Message)" }
    }
    Write-PipelineLog -Message "Invoking POST $BaseUrl/api/v1/bom" -Level TRACE
    $uploadResponse = Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/v1/bom" -Headers $dtHeaders -Form $form
    if (-not $uploadResponse.token) { throw "Failed to retrieve tracking token from Dependency-Track." }

    $uploadToken =$uploadResponse.token
    $isProcessing =$true
    $timeout = 60
    $stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
    while ($isProcessing -and $stopwatch.Elapsed.TotalSeconds -lt $timeout) {
        Start-Sleep -Seconds 2
        $tokenCheck = Invoke-RestMethod -Method Get -Uri "$BaseUrl/api/v1/bom/token/$uploadToken" -Headers $dtHeaders
        $isProcessing =$tokenCheck.processing
    }

    Write-PipelineLog -Message "Looking up Project UUID via GET $BaseUrl/api/v1/project/lookup?name=$encName&version=$encVersion" -Level TRACE
    $projectLookup = Invoke-RestMethod -Method Get -Uri $lookupUri -Headers $dtHeaders
    if (-not $projectLookup.uuid) { throw "Could not resolve Project UUID from Dependency-Track." }
    if (-not $projectLookup.lastBomImport -or [long]$projectLookup.lastBomImport -le $previousImport) {
        throw "Dependency-Track did not record a fresh BOM import for $ProjectName."
    }
    $bomImport = [long]$projectLookup.lastBomImport
    $projectUuid =$projectLookup.uuid

    try {
        $patchHeaders = @{ "X-Api-Key" = $ApiKey; "Content-Type" = "application/json"; "Accept" = "application/json" }
        $uiTags = @()
        foreach ($t in $Tags) { $uiTags += @{ name = $t } }
        $patchPayload = @{ description = $Description; tags = $uiTags } | ConvertTo-Json -Depth 3 -Compress
        Write-PipelineLog -Message "Patching metadata payload: $patchPayload" -Level TRACE
        Invoke-RestMethod -Method Patch -Uri "$BaseUrl/api/v1/project/$projectUuid" -Headers $patchHeaders -Body $patchPayload -ErrorAction Stop | Out-Null
    } catch {
        Write-PipelineLog -Message "Non-fatal error patching project UI metadata: $($_.Exception.Message)" -Level WARN
    }

    try {
        Invoke-RestMethod -Method Get -Uri "$BaseUrl/api/v1/finding/project/$projectUuid/export" -Headers $dtHeaders -OutFile $ExportPath
        Write-PipelineLog -Message "Server-side findings exported locally to: $ExportPath" -Level SUCCESS
    } catch {
        $errMsg =$_.Exception.Message
        if ($errMsg -match "403" -or $errMsg -match "Forbidden") {
            Write-PipelineLog -Message "403 Forbidden: Cannot export findings. RESOLUTION: Ensure your Dependency-Track API Key has the 'VIEW_VULNERABILITY' permission assigned, and has team access to the target project." -Level ERROR
        } else {
            Write-PipelineLog -Message "Failed to export vulnerability findings: $errMsg" -Level WARN
        }
        Set-PipelineFindingsExportFailure -Record $Record -Message $errMsg
    }

    Invoke-RestMethod -Method Get -Uri "$BaseUrl/api/v1/metrics/project/$projectUuid/refresh" -Headers $dtHeaders | Out-Null
    $metrics = $null
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        $metrics = Invoke-RestMethod -Method Get -Uri "$BaseUrl/api/v1/metrics/project/$projectUuid/current" -Headers $dtHeaders
        if ($metrics -and $metrics.lastOccurrence -and [long]$metrics.lastOccurrence -ge $bomImport) { break }
        Start-Sleep -Seconds 3
    }
    Test-PipelinePolicyGate -Metrics $metrics -ProjectName $ProjectName -RequiredOccurrence $bomImport
}
