[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$workspaceRoot = "C:\workspace"
# C:\engine is a second, separate mount of the generic engine's own root
# (the same convention windows_sbom_pipeline.ps1 uses) -- it holds
# Build-Sbom.ps1 even when $workspaceRoot is a different, real consumer
# repository. Falls back to $workspaceRoot for a standalone container run
# with no separate engine mount.
$engineRoot = if (Test-Path -LiteralPath "C:\engine") { "C:\engine" } else { $workspaceRoot }
$dependenciesRoot = Join-Path $workspaceRoot "dependencies"
$manifestPath = Join-Path $dependenciesRoot "vipm.toml"
$projectPath = Join-Path $dependenciesRoot "src\Dependencies.lvproj"
$outputDirectory = Join-Path $workspaceRoot "output\hosted-lvlibp"
$assetPath = Join-Path $outputDirectory "dependencies.lvlibp"
$supportPath = Join-Path $outputDirectory "support"
$sbomPath = Join-Path $outputDirectory "dependencies.lvlibp.cdx.sbom.json"
$buildLogPath = Join-Path $outputDirectory "dependencies-ppl-build.log"
$recordPath = Join-Path $outputDirectory "lvlibp-build-record.json"
$buildSpecName = "lba-plugins-dependencies-ppl"
. (Join-Path $engineRoot "Build-Sbom.ps1")

function Write-PipelineLog {
    param([Parameter(Mandatory = $true)][string]$Message, [string]$Level = "INFO")
    Write-Host "[$Level] $Message"
}

function ConvertTo-LabVIEWPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    if ($fullPath -notmatch '^(?<drive>[A-Za-z]):\\(?<tail>.*)$') {
        throw "LabVIEW project paths must be on a drive-letter volume: $fullPath"
    }
    return "/$($Matches.drive.ToUpperInvariant())/" + $Matches.tail.Replace('\', '/')
}

if (Test-Path -LiteralPath $outputDirectory) {
    Remove-Item -LiteralPath $outputDirectory -Recurse -Force
}
$null = New-Item -ItemType Directory -Path $supportPath -Force
$null = Start-Transcript -Path (Join-Path $outputDirectory "workflow-build.log")
try {
    foreach ($path in @($manifestPath, $projectPath)) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Required dependencies build input is missing: $path"
        }
    }

    $project = [System.Xml.XmlDocument]::new()
    $project.PreserveWhitespace = $true
    $project.Load($projectPath)
    $libraryItem = $project.SelectSingleNode("//Item[@Name='dependencies.lvlib' and @Type='Library']")
    if (-not $libraryItem -or $libraryItem.GetAttribute("URL") -cne "dependencies.lvlib") {
        throw "Dependencies project must reference its adjacent dependencies.lvlib."
    }
    $libraryPath = Join-Path (Split-Path -Parent $projectPath) "dependencies.lvlib"
    if (-not (Test-Path -LiteralPath $libraryPath -PathType Leaf)) {
        throw "Dependencies source library is missing: $libraryPath"
    }

    $library = [System.Xml.XmlDocument]::new()
    $library.Load($libraryPath)
    foreach ($item in $library.SelectNodes('//*[@URL]')) {
        $url = $item.GetAttribute("URL")
        if ([System.IO.Path]::IsPathRooted($url) -or $url -match '^[A-Za-z]:') {
            throw "Dependencies library contains an absolute source URL: $url"
        }
        $sourcePath = [System.IO.Path]::GetFullPath([System.IO.Path]::Combine($libraryPath, $url))
        $relativePath = [System.IO.Path]::GetRelativePath($dependenciesRoot, $sourcePath)
        if ([System.IO.Path]::IsPathRooted($relativePath) -or $relativePath -eq ".." -or
            $relativePath.StartsWith(".." + [System.IO.Path]::DirectorySeparatorChar)) {
            throw "Dependencies source URL escapes the repository: $url"
        }
        if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
            throw "Dependencies source URL is missing: $url"
        }
    }

    $sourceCommit = (& git -C $dependenciesRoot rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or $sourceCommit -notmatch '^[A-Fa-f0-9]{40}$') {
        throw "Could not resolve dependencies source commit."
    }
    $dependenciesRepository = $env:DEPENDENCIES_REPOSITORY
    if ([string]::IsNullOrWhiteSpace($dependenciesRepository)) {
        throw "DEPENDENCIES_REPOSITORY is required to identify the source repository in the generated evidence record."
    }
    $manifestHash = (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $vipmCliVersion = $env:VIPM_EXPECTED_CLI_VERSION
    $vipmDesktopVersion = $env:VIPM_EXPECTED_DESKTOP_VERSION
    if ([string]::IsNullOrWhiteSpace($vipmCliVersion) -or [string]::IsNullOrWhiteSpace($vipmDesktopVersion)) {
        throw "Hosted candidate requires the verified VIPM CLI and Desktop version pins."
    }
    $manifestText = Get-Content -LiteralPath $manifestPath -Raw -Encoding utf8
    if ($manifestText -match '(?im)^\s*\[nipm(?:\.[^\]]+)?\]\s*$') {
        throw "Hosted candidate manifests must not declare NIPM dependencies; the immutable NI base image supplies the LabVIEW runtime."
    }
    $labviewImageDigest = $env:LABVIEW_IMAGE_DIGEST
    if ($labviewImageDigest -notmatch '^nationalinstruments/labview@sha256:[A-Fa-f0-9]{64}$') {
        throw "Hosted candidate requires an immutable NI LabVIEW base image digest."
    }
    Copy-Item -LiteralPath $manifestPath -Destination (Join-Path $outputDirectory "vipm.toml")

    $projectSpec = $project.SelectSingleNode("//Item[@Name='$buildSpecName' and @Type='Packed Library']")
    if (-not $projectSpec) { throw "PPL build specification '$buildSpecName' was not found." }
    $properties = @{}
    foreach ($property in $projectSpec.SelectNodes("Property")) {
        $properties[$property.GetAttribute("Name")] = $property
    }
    foreach ($requiredProperty in @("Bld_autoIncrement", "Bld_localDestDir", "Destination[0].path", "Destination[1].path", "Bld_sbomFile", "Bld_userLogFile")) {
        if (-not $properties.ContainsKey($requiredProperty)) {
            throw "PPL build specification is missing '$requiredProperty'."
        }
    }
    if ($properties["Bld_autoIncrement"].InnerText -cne "false") {
        throw "PPL build specification must disable automatic version increment."
    }
    $properties["Bld_localDestDir"].InnerText = ConvertTo-LabVIEWPath $outputDirectory
    $properties["Destination[0].path"].InnerText = ConvertTo-LabVIEWPath $assetPath
    $properties["Destination[1].path"].InnerText = ConvertTo-LabVIEWPath $supportPath
    $properties["Bld_sbomFile"].InnerText = ConvertTo-LabVIEWPath $sbomPath
    $properties["Bld_userLogFile"].InnerText = ConvertTo-LabVIEWPath $buildLogPath
    $project.Save($projectPath)

    Push-Location -LiteralPath $dependenciesRoot
    try {
        & vipm --labview-version 2026 --labview-bitness 64 install --vipm -y
        $installExitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($installExitCode -ne 0) { throw "Pinned VIPM prerequisite installation failed with exit code $installExitCode." }

    $labviewVilibInfoJson = & vipm --labview-version 2026 --labview-bitness 64 info --nipm ni-labview-2026-vilib --installed-files --json
    $labviewVilibInfoExitCode = $LASTEXITCODE
    if ($labviewVilibInfoExitCode -ne 0) {
        throw "Could not inspect the NI LabVIEW VILib package in the base image; vipm exited with code $labviewVilibInfoExitCode."
    }
    $labviewVilibInfo = ($labviewVilibInfoJson -join [Environment]::NewLine) | ConvertFrom-Json
    if ([string]::IsNullOrWhiteSpace($labviewVilibInfo.installed_version)) {
        throw "The NI LabVIEW VILib package is not installed in the base image."
    }
    Write-Host "[INFO] Using base-image ni-labview-2026-vilib $($labviewVilibInfo.installed_version), pinned by $labviewImageDigest."

    $buildArguments = @(
        "--labview-version", "2026", "--labview-bitness", "64", "build", $projectPath,
        "--lvproj-build-spec", $buildSpecName, "--lvproj-target", "My Computer"
    )
    $buildExitCode = Invoke-VipmContainerBuild -Arguments $buildArguments -WorkingDirectory $dependenciesRoot
    if ($buildExitCode -ne 0) {
        $diagnosticPath = Join-Path $outputDirectory "missing-source-diagnostics.cdx.sbom.json"
        & vipm --labview-version 2026 --labview-bitness 64 sbom $projectPath `
            --format cyclonedx --schema-version 1.5 --lvproj-build-spec $buildSpecName `
            --allow-missing-files --output $diagnosticPath
        if ($LASTEXITCODE -ne 0) { Write-Warning "Diagnostic SBOM scan failed with exit code $LASTEXITCODE." }
        throw "VIPM PPL build failed with exit code $buildExitCode."
    }
    if (-not (Test-Path -LiteralPath $assetPath -PathType Leaf)) {
        throw "PPL build produced no LVLIBP at '$assetPath'."
    }

    & vipm --labview-version 2026 --labview-bitness 64 sbom $projectPath `
        --format cyclonedx --schema-version 1.5 --lvproj-build-spec $buildSpecName --output $sbomPath
    if ($LASTEXITCODE -ne 0) { throw "Strict PPL SBOM generation failed with exit code $LASTEXITCODE." }
    $bom = Get-Content -LiteralPath $sbomPath -Raw -Encoding utf8 | ConvertFrom-Json
    if ($bom.bomFormat -cne "CycloneDX" -or $bom.specVersion -cne "1.5" -or
        $bom.metadata.component.name -cne $buildSpecName -or [string]::IsNullOrWhiteSpace($bom.metadata.component.version)) {
        throw "PPL SBOM component identity does not match '$buildSpecName'."
    }
    $assetVersionInfo = [System.Diagnostics.FileVersionInfo]::GetVersionInfo($assetPath)
    Assert-ContainerPplVersion -FileVersion $assetVersionInfo.FileVersion -ProductVersion $assetVersionInfo.ProductVersion `
        -ExpectedVersion ([string]$bom.metadata.component.version)
    $vipmInventoryJson = & vipm --labview-version 2026 --labview-bitness 64 list --installed --json
    if ($LASTEXITCODE -ne 0) { throw "Could not inspect installed VIPM packages; vipm exited with code $LASTEXITCODE." }
    $vipmInventory = ($vipmInventoryJson -join [Environment]::NewLine) | ConvertFrom-Json
    $vipmPackageFiles = Get-ContainerVipmPackageFileInventory -Inventory $vipmInventory
    $pplSourceFiles = Read-ContainerPplSourceFile -BuildLogPath $buildLogPath
    $pplVipmPackages = Get-ContainerPplVipmSourcePackage -Inventory $vipmInventory `
        -PackageFiles $vipmPackageFiles -SourceFiles $pplSourceFiles `
        -RequiredPackages @("jdp_science_lib_common_utilities")
    $manifestSbomPath = Join-Path $outputDirectory "vipm-manifest.cdx.sbom.json"
    & vipm --labview-version 2026 --labview-bitness 64 sbom $manifestPath `
        --format cyclonedx --schema-version 1.5 --no-dev --output $manifestSbomPath
    if ($LASTEXITCODE -ne 0) { throw "VIPM manifest SBOM generation failed with exit code $LASTEXITCODE." }
    if (-not (Test-Path -LiteralPath $manifestSbomPath -PathType Leaf)) {
        throw "VIPM manifest SBOM generation did not produce '$manifestSbomPath'."
    }
    $manifestBom = Get-Content -LiteralPath $manifestSbomPath -Raw -Encoding utf8 | ConvertFrom-Json
    Merge-ContainerSbomVipmSourceComponent -Sbom $bom -ManifestSbom $manifestBom `
        -SourcePackages $pplVipmPackages | Out-Null
    Assert-ContainerSbomVipmCoverage -Sbom $bom -Inventory $vipmInventory -PackageFiles $vipmPackageFiles `
        -SourceFiles $pplSourceFiles -RequiredPackages @("jdp_science_lib_common_utilities") | Out-Null
    [System.IO.File]::WriteAllText($sbomPath, ($bom | ConvertTo-Json -Depth 100), [System.Text.UTF8Encoding]::new($false))

    $record = [ordered]@{
        candidateOnly = $true
        source = [ordered]@{
            repository = $dependenciesRepository
            commit = $sourceCommit
        }
        build = [ordered]@{
            spec = $buildSpecName
            labviewVersion = "2026"
            labviewBitness = "64"
            vipmCliVersion = $vipmCliVersion
            vipmDesktopVersion = $vipmDesktopVersion
            labviewBaseImageDigest = $labviewImageDigest
            labviewVilib = [ordered]@{
                installedVersion = $labviewVilibInfo.installed_version
                source = "preinstalled-base-image"
            }
            vipmManifestSha256 = $manifestHash
            vipmManifestSbomFileName = [System.IO.Path]::GetFileName($manifestSbomPath)
            vipmManifestSbomSha256 = (Get-FileHash -LiteralPath $manifestSbomPath -Algorithm SHA256).Hash.ToLowerInvariant()
            installedVipmPackages = @($vipmInventory.dependencies | ForEach-Object {
                [ordered]@{ name = $_.name; version = $_.installed_version }
            })
            pplVipmPackages = @($pplVipmPackages.GetEnumerator() | ForEach-Object {
                [ordered]@{ name = $_.Key; version = $_.Value.version; sourceFiles = @($_.Value.sourceFiles) }
            })
            componentName = $bom.metadata.component.name
            componentVersion = $bom.metadata.component.version
        }
        asset = [ordered]@{
            fileName = [System.IO.Path]::GetFileName($assetPath)
            fileVersion = $assetVersionInfo.FileVersion
            productVersion = $assetVersionInfo.ProductVersion
            sizeBytes = (Get-Item -LiteralPath $assetPath).Length
            sha256 = (Get-FileHash -LiteralPath $assetPath -Algorithm SHA256).Hash.ToLowerInvariant()
        }
        sbom = [ordered]@{
            fileName = [System.IO.Path]::GetFileName($sbomPath)
            sizeBytes = (Get-Item -LiteralPath $sbomPath).Length
            sha256 = (Get-FileHash -LiteralPath $sbomPath -Algorithm SHA256).Hash.ToLowerInvariant()
        }
        run = [ordered]@{
            id = $env:CANDIDATE_RUN_ID
            url = $env:CANDIDATE_RUN_URL
        }
    }
    [System.IO.File]::WriteAllText($recordPath,
        ($record | ConvertTo-Json -Depth 10), [System.Text.UTF8Encoding]::new($false))
} finally {
    Stop-Transcript | Out-Null
}