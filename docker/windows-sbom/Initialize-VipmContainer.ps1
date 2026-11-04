function Resolve-VipmContainerCommandLine {
    [CmdletBinding()]
    param(
        [AllowNull()][object[]]$Arguments = @(),
        [Parameter(Mandatory)][string]$DefaultCommand
    )
    if ($null -eq $Arguments -or $Arguments.Count -eq 0) {
        $commandLine = @($DefaultCommand)
    } else {
        $commandLine = @($Arguments)
    }
    $commandArguments = @()
    if ($commandLine.Count -gt 1) {
        $commandArguments = @($commandLine[1..($commandLine.Count - 1)])
    }
    return [pscustomobject]@{
        Command = $commandLine[0]
        Arguments = $commandArguments
    }
}

function Invoke-VipmContainerBuildOnce {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory
    )

    $buildOutput = [System.Collections.Generic.List[string]]::new()
    $vipmPath = (Get-Command vipm -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    $exitCode = Invoke-StreamingProcess -FilePath $vipmPath -ArgumentList $Arguments `
        -WorkingDirectory $WorkingDirectory -OnLine {
            param([string]$line, [bool]$isStderr)
            $buildOutput.Add($line)
            $visibleLine = if ($isStderr) { "[VIPM stderr] $line" } else { $line }
            Write-Host $visibleLine
        }
    return [pscustomobject]@{
        ExitCode = $exitCode
        Output = $buildOutput -join [Environment]::NewLine
    }
}

function Invoke-VipmContainerBuild {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory
    )

    $buildResult = Invoke-VipmContainerBuildOnce -Arguments $Arguments -WorkingDirectory $WorkingDirectory
    if ($buildResult.ExitCode -eq 0) { return 0 }

    $labviewProcesses = @(Get-Process -Name LabVIEW -ErrorAction SilentlyContinue)
    if ($buildResult.Output -notmatch '\(code 1130\)' -or $labviewProcesses.Count -eq 0) {
        return $buildResult.ExitCode
    }

    Write-Warning "vipm build failed with code 1130 after launching LabVIEW; retrying once with LabVIEW already running."
    $retryResult = Invoke-VipmContainerBuildOnce -Arguments $Arguments -WorkingDirectory $WorkingDirectory
    if ($retryResult.ExitCode -ne 0) {
        throw "$($retryResult.Output) (Failed again after a retry with LabVIEW already running; the error above is App Builder's own error.)"
    }
    return 0
}

function Assert-ContainerPplVersion {
    [CmdletBinding()]
    param(
        [AllowEmptyString()][string]$FileVersion,
        [AllowEmptyString()][string]$ProductVersion,
        [Parameter(Mandatory = $true)][string]$ExpectedVersion
    )

    if ([string]::IsNullOrWhiteSpace($FileVersion) -or [string]::IsNullOrWhiteSpace($ProductVersion)) {
        throw "Packed library version information is missing."
    }
    if ($FileVersion -cne $ExpectedVersion -or $ProductVersion -cne $ExpectedVersion) {
        throw "Packed library file version '$FileVersion' and product version '$ProductVersion' do not match SBOM version '$ExpectedVersion'."
    }
}

function ConvertTo-ContainerPathKey {
    param([Parameter(Mandatory = $true)][string]$Path)

    $normalized = $Path.Trim().Trim('"').Replace('/', '\')
    if ([string]::IsNullOrWhiteSpace($normalized) -or $normalized -notmatch '^(?:[A-Za-z]:\\|\\\\)') {
        throw "Installed-file inventory contains a non-absolute path: '$Path'."
    }
    if ($IsWindows) { $normalized = [System.IO.Path]::GetFullPath($normalized) }
    return $normalized.TrimEnd('\').ToUpperInvariant()
}

function Get-ContainerVipmInstalledFilePath {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)]$PackageInfo)

    $fileCollections = [System.Collections.Generic.List[object]]::new()
    $visit = {
        param($Value)
        if ($Value -is [System.Collections.IDictionary]) {
            foreach ($key in $Value.Keys) {
                $normalizedKey = ([string]$key -replace '[^A-Za-z]', '').ToLowerInvariant()
                if ($normalizedKey -in @('installedfiles', 'installedfilepaths', 'files')) {
                    $fileCollections.Add($Value[$key])
                } elseif ($Value[$key] -is [System.Collections.IDictionary] -or
                    ($Value[$key] -is [System.Collections.IEnumerable] -and $Value[$key] -isnot [string])) {
                    & $visit $Value[$key]
                }
            }
        } elseif ($Value -is [System.Collections.IEnumerable] -and $Value -isnot [string]) {
            foreach ($item in $Value) { & $visit $item }
        }
    }
    & $visit $PackageInfo
    if ($fileCollections.Count -ne 1) {
        throw "VIPM package information must contain exactly one installed-files collection."
    }

    $paths = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($entry in @($fileCollections[0])) {
        $path = if ($entry -is [string]) { $entry } else {
            foreach ($field in @('path', 'filePath', 'file_path', 'fullPath', 'full_path', 'file')) {
                if ($entry -is [System.Collections.IDictionary] -and $entry.Contains($field) -and
                    -not [string]::IsNullOrWhiteSpace([string]$entry[$field])) {
                    [string]$entry[$field]
                    break
                }
            }
        }
        if ([string]::IsNullOrWhiteSpace($path)) { throw "VIPM installed-files collection contains an invalid entry." }
        [void]$paths.Add((ConvertTo-ContainerPathKey -Path $path))
    }
    if ($paths.Count -eq 0) { throw "VIPM package information returned no installed files." }
    return ,([string[]]@($paths))
}

function Read-ContainerPplSourceFile {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$BuildLogPath)

    $sourceFiles = [System.Collections.Generic.Dictionary[string, string]]::new([StringComparer]::OrdinalIgnoreCase)
    $inSourceFiles = $false
    foreach ($line in [System.IO.File]::ReadAllLines($BuildLogPath)) {
        if ($line -match '^\s*\[([^\]]+)\]\s*$') {
            if ($inSourceFiles) { break }
            $inSourceFiles = $Matches[1] -ceq 'Source Files'
            continue
        }
        if (-not $inSourceFiles -or [string]::IsNullOrWhiteSpace($line)) { continue }
        if ($line -notmatch '^(?<path>.+?)\s+\d{1,2}/\d{1,2}/\d{4}\s') {
            throw "PPL build log contains an unreadable Source Files entry: '$line'."
        }
        $path = $Matches['path'].Trim()
        $sourceFiles[(ConvertTo-ContainerPathKey -Path $path)] = $path
    }
    if (-not $inSourceFiles -or $sourceFiles.Count -eq 0) {
        throw "PPL build log is missing a non-empty [Source Files] section."
    }
    return $sourceFiles
}

function Get-ContainerVipmPackageFileInventory {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)]$Inventory)

    if ($null -eq $Inventory.dependencies -or @($Inventory.dependencies).Count -eq 0) {
        throw "VIPM installed-package inventory is missing or empty."
    }
    $filesByPackage = [System.Collections.Generic.Dictionary[string, object]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($package in @($Inventory.dependencies)) {
        $name = [string]$package.name
        if ([string]::IsNullOrWhiteSpace($name)) { throw "VIPM installed-package inventory has a package with no name." }
        if ($package.status -cne 'installed') { throw "VIPM inventory package '$name' does not have status installed." }
        if ([string]::IsNullOrWhiteSpace([string]$package.installed_version)) {
            throw "VIPM installed-package inventory has no version for '$name'."
        }
        if ($filesByPackage.ContainsKey($name)) { throw "VIPM installed-package inventory has duplicate package '$name'." }
        $packageInfoJson = & vipm --labview-version 2026 --labview-bitness 64 --json info $name --installed-files
        if ($LASTEXITCODE -ne 0) { throw "Could not inspect installed files for VIPM package '$name'; vipm exited with code $LASTEXITCODE." }
        try {
            $packageInfo = ($packageInfoJson -join [Environment]::NewLine) | ConvertFrom-Json -AsHashtable -ErrorAction Stop
        } catch {
            throw "VIPM installed-file response for '$name' is invalid JSON: $($_.Exception.Message)"
        }
        $filesByPackage.Add($name, (Get-ContainerVipmInstalledFilePath -PackageInfo $packageInfo))
    }
    return $filesByPackage
}

function Get-ContainerPplVipmSourcePackage {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Inventory,
        [Parameter(Mandatory = $true)]$PackageFiles,
        [Parameter(Mandatory = $true)]$SourceFiles,
        [string[]]$RequiredPackages = @()
    )

    if ($null -eq $Inventory.dependencies -or @($Inventory.dependencies).Count -eq 0) {
        throw "VIPM installed-package inventory is missing or empty."
    }
    if ($null -eq $SourceFiles -or $SourceFiles.Count -eq 0) { throw "PPL source-file inventory is missing or empty." }
    $names = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    $ownersBySource = [System.Collections.Generic.Dictionary[string, string]]::new([StringComparer]::OrdinalIgnoreCase)
    $sourcePackages = [System.Collections.Generic.Dictionary[string, object]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($package in @($Inventory.dependencies)) {
        $name = [string]$package.name
        $version = [string]$package.installed_version
        if ([string]::IsNullOrWhiteSpace($name) -or [string]::IsNullOrWhiteSpace($version) -or -not $names.Add($name)) {
            throw "VIPM installed-package inventory has a missing or duplicate package identity."
        }
        if ($package.status -cne "installed") { throw "VIPM inventory package '$name' does not have status installed." }
        if (-not $PackageFiles.ContainsKey($name) -or @($PackageFiles[$name]).Count -eq 0) {
            throw "Installed VIPM package '$name' has no authoritative installed-file inventory."
        }
        $ownedFiles = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
        foreach ($path in @($PackageFiles[$name])) { [void]$ownedFiles.Add((ConvertTo-ContainerPathKey -Path ([string]$path))) }
        foreach ($sourceKey in $SourceFiles.Keys) {
            if (-not $ownedFiles.Contains([string]$sourceKey)) { continue }
            if ($ownersBySource.ContainsKey([string]$sourceKey)) {
                throw "PPL source file '$($SourceFiles[$sourceKey])' has ambiguous VIPM package ownership."
            }
            $ownersBySource.Add([string]$sourceKey, $name)
            if (-not $sourcePackages.ContainsKey($name)) {
                $sourcePackages.Add($name, [ordered]@{ version = $version; sourceFiles = [System.Collections.Generic.List[string]]::new() })
            }
            $sourcePackages[$name].sourceFiles.Add([string]$SourceFiles[$sourceKey])
        }
    }
    foreach ($requiredPackage in $RequiredPackages) {
        if (-not $sourcePackages.ContainsKey($requiredPackage)) {
            throw "Required VIPM package '$requiredPackage' owns no PPL source files."
        }
    }
    return $sourcePackages
}

function Merge-ContainerSbomVipmSourceComponent {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Sbom,
        [Parameter(Mandatory = $true)]$ManifestSbom,
        [Parameter(Mandatory = $true)]$SourcePackages
    )

    if ($ManifestSbom.bomFormat -cne 'CycloneDX' -or $ManifestSbom.specVersion -cne '1.5') {
        throw 'VIPM manifest SBOM must be CycloneDX 1.5.'
    }
    $rootRef = [string]$Sbom.metadata.component.'bom-ref'
    if ([string]::IsNullOrWhiteSpace($rootRef)) { throw 'PPL SBOM product reference is missing.' }
    $components = [System.Collections.Generic.List[object]]::new()
    if ($Sbom.components) { $components.AddRange([object[]]@($Sbom.components)) }
    $knownRefs = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    [void]$knownRefs.Add($rootRef)
    foreach ($component in $components) {
        $reference = [string]$component.'bom-ref'
        if ([string]::IsNullOrWhiteSpace($reference) -or -not $knownRefs.Add($reference)) {
            throw 'PPL SBOM component references must be present and unique before enrichment.'
        }
    }
    $dependencies = [System.Collections.Generic.List[object]]::new()
    if ($Sbom.dependencies) { $dependencies.AddRange([object[]]@($Sbom.dependencies)) }
    $rootNodes = @($dependencies | Where-Object { $_.ref -ceq $rootRef })
    if ($rootNodes.Count -ne 1) { throw 'PPL SBOM must contain exactly one product dependency node.' }
    $rootDependsOn = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    foreach ($reference in @($rootNodes[0].dependsOn)) { if ($reference) { [void]$rootDependsOn.Add([string]$reference) } }

    foreach ($name in $sourcePackages.Keys) {
        $version = [string]$sourcePackages[$name].version
        $expectedPurl = 'pkg:vipm/{0}@{1}' -f [uri]::EscapeDataString($name.ToLowerInvariant()), [uri]::EscapeDataString($version)
        $manifestComponents = @($ManifestSbom.components | Where-Object {
            $_.name -ieq $name -and $_.version -ceq $version -and $_.purl -ceq $expectedPurl
        })
        if ($manifestComponents.Count -ne 1) {
            $availableComponents = @($ManifestSbom.components)
            $available = @($availableComponents | ForEach-Object { "$($_.name)@$($_.version) [$($_.purl)]" }) -join '; '
            throw "Installed VIPM package '$name' version '$version' expects PURL '$expectedPurl', but the VIPM manifest SBOM has $($manifestComponents.Count) exact matches. Available components: $available"
        }
        $matchingComponents = @($components | Where-Object {
            $_.name -ieq $name -and $_.version -ceq $version -and $_.purl -ceq $expectedPurl
        })
        if ($matchingComponents.Count -gt 1) {
            throw "Installed VIPM package '$name' version '$version' is ambiguous in the PPL SBOM."
        }
        if ($matchingComponents.Count -eq 1) {
            $componentRef = [string]$matchingComponents[0].'bom-ref'
        } else {
            $componentRef = [string]$manifestComponents[0].'bom-ref'
            if ([string]::IsNullOrWhiteSpace($componentRef) -or -not $knownRefs.Add($componentRef)) {
                throw "VIPM manifest component '$name' has a missing or duplicate bom-ref."
            }
            $components.Add($manifestComponents[0])
        }
        if ([string]::IsNullOrWhiteSpace($componentRef)) { throw "VIPM component '$name' has no bom-ref." }
        [void]$rootDependsOn.Add($componentRef)
    }
    $rootNodes[0].dependsOn = @($rootDependsOn)
    $Sbom.components = $components.ToArray()
    $Sbom.dependencies = $dependencies.ToArray()
    return $Sbom
}

function Assert-ContainerSbomVipmCoverage {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Sbom,
        [Parameter(Mandatory = $true)]$Inventory,
        [Parameter(Mandatory = $true)]$PackageFiles,
        [Parameter(Mandatory = $true)]$SourceFiles,
        [string[]]$RequiredPackages = @()
    )

    $sourcePackages = Get-ContainerPplVipmSourcePackage -Inventory $Inventory -PackageFiles $PackageFiles `
        -SourceFiles $SourceFiles -RequiredPackages $RequiredPackages
    $rootRef = [string]$Sbom.metadata.component.'bom-ref'
    if ([string]::IsNullOrWhiteSpace($rootRef)) { throw 'SBOM product reference is missing.' }
    $componentRefs = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    [void]$componentRefs.Add($rootRef)
    foreach ($component in @($Sbom.components)) {
        $reference = [string]$component.'bom-ref'
        if ([string]::IsNullOrWhiteSpace($reference) -or -not $componentRefs.Add($reference)) {
            throw 'SBOM component reference must be present and unique.'
        }
    }
    $nodes = [System.Collections.Generic.Dictionary[string, object]]::new([StringComparer]::Ordinal)
    foreach ($node in @($Sbom.dependencies)) {
        $reference = [string]$node.ref
        if ([string]::IsNullOrWhiteSpace($reference) -or $nodes.ContainsKey($reference)) {
            throw 'SBOM dependency graph has a missing or duplicate reference.'
        }
        $nodes.Add($reference, $node)
    }
    if (-not $nodes.ContainsKey($rootRef)) { throw 'SBOM dependency graph has no product node.' }
    $reachable = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    $queue = [System.Collections.Generic.Queue[string]]::new()
    $queue.Enqueue($rootRef)
    while ($queue.Count -gt 0) {
        $reference = $queue.Dequeue()
        if (-not $reachable.Add($reference) -or -not $nodes.ContainsKey($reference)) { continue }
        foreach ($child in @($nodes[$reference].dependsOn)) {
            if (-not [string]::IsNullOrWhiteSpace([string]$child)) { $queue.Enqueue([string]$child) }
        }
    }
    foreach ($name in $sourcePackages.Keys) {
        $version = [string]$sourcePackages[$name].version
        $expectedPurl = 'pkg:vipm/{0}@{1}' -f [uri]::EscapeDataString($name.ToLowerInvariant()), [uri]::EscapeDataString($version)
        $matchingComponents = @($Sbom.components | Where-Object {
            $_.name -ieq $name -and $_.version -ceq $version -and $_.purl -ceq $expectedPurl
        })
        if ($matchingComponents.Count -ne 1) { throw "Installed VIPM package '$name' version '$version' is missing or ambiguous in the SBOM." }
        $componentRef = [string]$matchingComponents[0].'bom-ref'
        if ([string]::IsNullOrWhiteSpace($componentRef) -or -not $reachable.Contains($componentRef)) {
            throw "Installed VIPM package '$name' is not reachable from the SBOM product."
        }
    }
    return $sourcePackages
}

function Start-VipmContainerDesktop {
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute("PSUseShouldProcessForStateChangingFunctions", "", Justification = "Internal startup in a disposable container.")]
    param([Parameter(Mandatory)][string]$DesktopPath)
    $previous = $env:LV_RTE_HEADLESS
    try {
        # Only Desktop and its LabVIEW children inherit this, not CLI/File Handler.
        $env:LV_RTE_HEADLESS = "1"
        $desktop = Start-Process -FilePath $DesktopPath -PassThru -ErrorAction Stop
        if ($desktop.HasExited) { throw "VIPM Desktop exited immediately with code $($desktop.ExitCode)." }
    } finally {
        if ($null -eq $previous) {
            Remove-Item Env:LV_RTE_HEADLESS -ErrorAction SilentlyContinue
        } else {
            $env:LV_RTE_HEADLESS = $previous
        }
    }
}

function Start-VipmContainerLabview {
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute("PSUseShouldProcessForStateChangingFunctions", "", Justification = "Internal startup in a disposable container.")]
    param(
        [Parameter(Mandatory)][string]$LabviewPath,
        [Parameter(Mandatory)][int]$Port,
        [switch]$Restart
    )
    if ($Restart) {
        foreach ($process in @(Get-Process -Name LabVIEW -ErrorAction SilentlyContinue |
                Where-Object { $_.Path -eq $LabviewPath })) {
            Stop-Process -Id $process.Id -Force -ErrorAction Stop
            if (-not $process.WaitForExit(120000)) { throw "Container LabVIEW PID $($process.Id) did not exit." }
        }
    }
    $labview = Start-Process -FilePath $LabviewPath -ArgumentList "--headless" -PassThru -ErrorAction Stop
    $deadline = [DateTime]::UtcNow.AddSeconds(120)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($labview.HasExited) { throw "Headless LabVIEW exited before VI Server became ready." }
        $tcp = [Net.Sockets.TcpClient]::new()
        try {
            $pending = $tcp.ConnectAsync("127.0.0.1", $Port)
            if ($pending.Wait(1000) -and $tcp.Connected) { return }
        } catch [System.AggregateException] {
            if ($_.Exception.GetBaseException() -isnot [Net.Sockets.SocketException]) { throw }
        } finally {
            $tcp.Dispose()
        }
        Start-Sleep -Seconds 1
    }
    throw "LabVIEW VI Server on port $Port did not become ready within 120 seconds."
}

function Initialize-VipmContainer {
    if ($env:LV_RTE_HEADLESS -eq "1") {
        throw "LV_RTE_HEADLESS must not be set globally: VIPM File Handler cannot use Desktop's headless override."
    }
    $text = & vipm labview list --json --color-mode never
    if ($LASTEXITCODE -ne 0) { throw "VIPM LabVIEW discovery failed with exit code $LASTEXITCODE." }
    $targets = @((ConvertFrom-Json -InputObject ($text -join [Environment]::NewLine)).labview_versions)
    if ($targets.Count -ne 1) { throw "Expected exactly one LabVIEW target in the Windows container; discovered $($targets.Count)." }
    $target = $targets[0]
    if (-not $target.vi_server_enabled -or [int]$target.vi_server_tcp_port -notin 1..65535) {
        throw "LabVIEW VI Server TCP must be enabled in the container image."
    }
    if (-not (Test-Path -LiteralPath $target.app_path -PathType Leaf) -or
        $target.bitness -notin 32, 64 -or $target.version_number -notmatch '^(\d+\.\d+)(?:\.|$)') {
        throw "VIPM returned an invalid LabVIEW executable or version."
    }
    $apiVersion = "$($Matches[1]) ($($target.bitness)-bit)"
    $port = [int]$target.vi_server_tcp_port
    $settingsPath = Join-Path $env:ProgramData "JKI\VIPM\Settings.ini"
    if (Test-Path -LiteralPath $settingsPath) {
        throw "Expected a fresh container without VIPM Settings.ini; refusing to overwrite existing target settings."
    }
    $null = New-Item -ItemType Directory -Path (Split-Path $settingsPath) -Force
    $location = "/" + ($target.app_path -replace ':', '' -replace '\\', '/')
    $settings = @"
[General]
check for updates on startup?="FALSE"

[Targets]
Names.<size(s)>="1"
Names 0="LabVIEW"
Versions.<size(s)>="1"
Versions 0="$apiVersion"
Locations.<size(s)>="1"
Locations 0="$location"
Ports="<size(s)=1> $port"
Tested.<size(s)>="1"
Tested 0="TRUE"
Disabled.<size(s)>="1"
Disabled 0="FALSE"
CommunityEdition.<size(s)>="1"
CommunityEdition 0="FALSE"
Active Target.Name="LabVIEW"
Active Target.Version="$apiVersion"
Connection Timeout="120"
PingDelay(ms)="-1"
PingTimeout(ms)="120000"
"@
    [IO.File]::WriteAllText($settingsPath, $settings)
    $env:SBOM_CONTAINER_LABVIEW_PATH = $target.app_path
    $env:SBOM_CONTAINER_LABVIEW_PORT = [string]$port
    Start-VipmContainerLabview -LabviewPath $target.app_path -Port $port
    $cliPath = (Get-Command vipm -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    $desktopPath = Join-Path (Split-Path (Split-Path $cliPath -Parent) -Parent) "VI Package Manager.exe"
    $env:VIPM_DESKTOP_LIVELINESS_TIMEOUT = "240"
    $env:VIPM_TIMEOUT = "900"
    Start-VipmContainerDesktop -DesktopPath $desktopPath
    Write-Host "[INFO] VIPM target $apiVersion ready on port $port; Desktop has scoped headless inheritance."
}

function Update-ContainerNipmFeed {
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute("PSUseShouldProcessForStateChangingFunctions", "", Justification = "Explicit prerequisite provisioning in a disposable container.")]
    param(
        [System.Collections.IDictionary]$Feeds,
        [Parameter(Mandatory)][string]$NipkgPath
    )
    if ($null -eq $Feeds -or $Feeds.Count -eq 0) { return }
    $listing = Invoke-Native -Arguments @($NipkgPath, "feed-list") -Capture
    $existing = @{}
    foreach ($line in ($listing -split '\r?\n')) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        if ($line -notmatch '^(\S+)\s+(\S+)\s*$') { throw "Invalid NIPM feed listing: '$line'." }
        $existing[$Matches[1]] = $Matches[2].TrimEnd('/')
    }
    foreach ($name in $Feeds.Keys) {
        $url = ([string]$Feeds[$name]).Trim().TrimEnd('/')
        if ($existing.ContainsKey($name)) {
            if ($existing[$name] -ne $url) { throw "NIPM feed '$name' is already registered with a different URL." }
        } elseif ($existing.Values -notcontains $url) {
            Invoke-Native -Arguments @($NipkgPath, "feed-add", "--name=$name", $url)
        }
    }
    Invoke-Native -Arguments @($NipkgPath, "update")
}

function Install-ContainerBuildDependency {
    param([string]$ProjectDir, [hashtable]$Manifest, $Project, [string]$LabviewVersion, [string]$LabviewBitness)
    $target = @("--labview-version", $LabviewVersion, "--labview-bitness", $LabviewBitness)
    Invoke-Vipm -Arguments (@("install", "--vipm", "-y") + $target) -WorkingDirectory $ProjectDir -ShowProgress
    if ($Manifest.ContainsKey("nipm.dependencies") -and $Manifest["nipm.dependencies"].Count -gt 0) {
        Update-ContainerNipmFeed -Feeds $Project["windowsContainerNipmFeeds"] -NipkgPath $NipkgPath
        Invoke-Vipm -Arguments (@("install", "--nipm", "-y") + $target) -WorkingDirectory $ProjectDir -ShowProgress
        foreach ($name in $Manifest["nipm.dependencies"].Keys) {
            $info = Invoke-Vipm -Arguments @("info", "--nipm", $name, "--installed-files") -Json | ConvertFrom-Json
            if ($info.installed_version -ne $Manifest["nipm.dependencies"][$name]) {
                throw "NI build prerequisite '$name' installed version does not match the manifest."
            }
            if ($name -eq "ni-syscfg-labview-support") {
                $api = "C:\Program Files\NI\LVAddons\nisyscfg\1\vi.lib\nisyscfg"
                foreach ($file in "Initialize Session.vi", "Find Systems.vi", "Find Hardware.vi", "Close (System).vi") {
                    $path = Join-Path $api $file
                    if ($info.installed_files -notcontains $path -or -not (Test-Path -LiteralPath $path -PathType Leaf)) {
                        throw "Missing NI System Configuration API payload '$path'."
                    }
                }
            }
        }
        Start-VipmContainerLabview -LabviewPath $env:SBOM_CONTAINER_LABVIEW_PATH -Port ([int]$env:SBOM_CONTAINER_LABVIEW_PORT) -Restart
    }
}
