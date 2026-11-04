<#
.SYNOPSIS
    Generates a Linux SBOM for a LabVIEW project inside a Docker container.

.DESCRIPTION
    Builds docker/linux-sbom (NI's official LabVIEW Linux image + VIPM CLI +
    Grype) with the local Docker engine, for example Docker Desktop in Linux
    containers mode, then runs the container pipeline against this repository
    mounted at /workspace. The VIT Super Network Streams sample is the default.

    Every run pulls -LabVIEWImage (default nationalinstruments/labview:latest-linux),
    resolves it to a digest, and rebuilds the image without cache from that
    digest. The SBOM records both the tag and the digest.

    VIPM Pro activation is required. Provide VIPM_SERIAL_NUMBER, VIPM_FULL_NAME
    and VIPM_EMAIL as process environment variables or in the -EnvFile file
    (default: docker/linux-sbom/.env, ignored by Git). Values in the file take
    precedence over process environment variables.

    SVN_GLOBAL_USER and SVN_GLOBAL_PASSWORD, from the env file or the process
    environment, are passed to the container and used for SVN projects whose
    configuration supplies no username and password or token.

    DEPENDENCY_TRACK_MODE=full (default) requires the API key, reachable backend,
    and fresh Critical/High metrics. hosted-grype explicitly skips the upload.

    The run also fails when Grype reports findings at or above GRYPE_FAIL_ON
    (default: high; "none" reports only). Grype uses Ubuntu's advisories and
    is the authoritative gate for OS packages.

    Reports are written to output/linux/<project-name>/.

.EXAMPLE
    .\Run-LinuxSbom.ps1

.EXAMPLE
    .\Run-LinuxSbom.ps1 -SkipScan -SkipDependencyTrack

.EXAMPLE
    # Without VIPM Pro: base SBOM from vipm.toml plus the Linux dpkg deep scan.
    .\Run-LinuxSbom.ps1 -ManifestOnly
#>
[CmdletBinding()]
param(
    # No built-in default -- this is a generic engine shared by any fork;
    # SandboxSBOMReporting's own real project is only ever supplied via
    # PROJECTS_OVERRIDE (CI) or an explicit -ConfigName/-VipmTomlPath
    # (documented local invocation example), never hardcoded here.
    [string]$ConfigName = "",
    [string]$VipmTomlPath = "",
    [string]$EnvFile = "docker/linux-sbom/.env",
    [string]$Image = "sandbox-sbom-linux:latest",
    [string]$LabVIEWImage = "nationalinstruments/labview:latest-linux",
    [string]$OutputDir = "output/linux",
    [switch]$SkipScan,
    [switch]$NiPackagesOnly,
    [switch]$ManifestOnly,
    [switch]$SkipDependencyTrack
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message, [string]$Color = "Cyan")
    Write-Host "[$((Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))] $Message" -ForegroundColor $Color
}

function ConvertTo-RepoRelativePath {
    param([string]$Path, [string]$Name)
    $full = [System.IO.Path]::GetFullPath([System.IO.Path]::Combine($PSScriptRoot, $Path))
    $root = [System.IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    if (-not $full.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Name must be inside the repository: '$Path'."
    }
    return $full.Substring($root.Length).Replace('\', '/')
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker CLI not found. Install and start Docker Desktop."
}
$engineOs = docker info --format '{{.OSType}}' 2>$null
if ($LASTEXITCODE -ne 0) { throw "Docker engine is not reachable. Start Docker Desktop." }
if ($engineOs -ne "linux") {
    throw "Docker engine OS is '$engineOs'. Switch Docker Desktop to Linux containers."
}

$outputRelative = ConvertTo-RepoRelativePath -Path $OutputDir -Name "OutputDir"
$schemaText = [System.IO.File]::ReadAllText([System.IO.Path]::Combine($PSScriptRoot, "pipeline-config.schema.json"))
# PROJECTS_OVERRIDE and VIPM_TOML_CONFIGS (as set by CI) replace -ConfigName and -VipmTomlPath.
$configArgs = @()
$configText = $env:PROJECTS_OVERRIDE
if ([string]::IsNullOrWhiteSpace($configText)) {
    if ([string]::IsNullOrWhiteSpace($ConfigName)) {
        throw "Set the process-level PROJECTS_OVERRIDE variable or pass -ConfigName with a local configuration file."
    }
    $configRelative = ConvertTo-RepoRelativePath -Path $ConfigName -Name "ConfigName"
    $configText = [System.IO.File]::ReadAllText([System.IO.Path]::Combine($PSScriptRoot, $configRelative))
    $configArgs += @("-e", "PIPELINE_CONFIG=$configRelative")
} else {
    $configRelative = "PROJECTS_OVERRIDE"
    $configArgs += @("-e", "PROJECTS_OVERRIDE")
}
if (-not (Test-Json -Json $configText -Schema $schemaText)) {
    throw "'$configRelative' does not match pipeline-config.schema.json."
}
if ([string]::IsNullOrWhiteSpace($env:VIPM_TOML_CONFIGS)) {
    if ([string]::IsNullOrWhiteSpace($VipmTomlPath)) {
        throw "Set the process-level VIPM_TOML_CONFIGS variable or pass -VipmTomlPath with a local VIPM manifest file."
    }
    $tomlRelative = ConvertTo-RepoRelativePath -Path $VipmTomlPath -Name "VipmTomlPath"
    if (-not (Test-Path -LiteralPath ([System.IO.Path]::Combine($PSScriptRoot, $tomlRelative)) -PathType Leaf)) {
        throw "VIPM manifest '$tomlRelative' does not exist."
    }
    $configArgs += @("-e", "VIPM_TOML=$tomlRelative")
} else {
    $configArgs += @("-e", "VIPM_TOML_CONFIGS")
}

$credentialArgs = @()
$envFilePath = [System.IO.Path]::Combine($PSScriptRoot, $EnvFile)
$envFileNames = @()
if (Test-Path -LiteralPath $envFilePath -PathType Leaf) {
    Write-Step "Using settings from '$EnvFile'."
    $credentialArgs += @("--env-file", $envFilePath)
    # Names with a non-empty value in the file; values are never read into this script's output.
    $envFileNames = @(Get-Content -LiteralPath $envFilePath |
        Where-Object { $_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)=\s*\S' } |
        ForEach-Object { $Matches[1] })
}

function Add-ProcessEnvironmentArg {
    param([string]$Name)
    # Passing only the name makes Docker read the value from this process,
    # keeping secrets off the command line. A value in the env file wins.
    if ($envFileNames -contains $Name) { return $true }
    if (-not [string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($Name))) {
        if ($script:credentialArgs -notcontains $Name) { $script:credentialArgs += @("-e", $Name) }
        return $true
    }
    return $false
}

if ($ManifestOnly) {
    Write-Step "ManifestOnly: building the base SBOM from the VIPM manifest without VIPM Pro." -Color DarkYellow
} else {
    foreach ($name in "VIPM_SERIAL_NUMBER", "VIPM_FULL_NAME", "VIPM_EMAIL") {
        if (-not (Add-ProcessEnvironmentArg -Name $name)) {
            throw "Set $name or add it to '$EnvFile' (see docker/linux-sbom/.env.example). VIPM Pro is required for 'vipm sbom'; use -ManifestOnly to run without it."
        }
    }
}

[void](Add-ProcessEnvironmentArg -Name "GRYPE_FAIL_ON")

# SVN credentials for projects whose configuration supplies none: the env file wins over the process environment.
$svnSources = foreach ($name in "SVN_GLOBAL_USER", "SVN_GLOBAL_PASSWORD") {
    if ($envFileNames -contains $name) { "$name (env file)" }
    elseif (Add-ProcessEnvironmentArg -Name $name) { "$name (environment)" }
}
if ($svnSources) { Write-Step "Passing SVN credentials: $($svnSources -join ', ')." }

# Other credentials the configuration references as "$env:NAME".
foreach ($name in ([regex]::Matches($configText, '\$env:([A-Za-z_][A-Za-z0-9_]*)') | ForEach-Object { $_.Groups[1].Value } | Sort-Object -Unique)) {
    if (-not (Add-ProcessEnvironmentArg -Name $name)) {
        Write-Step "The configuration references `$env:$name, which is not set; that value resolves to empty." -Color DarkYellow
    }
}

$reportingMode = if ([string]::IsNullOrWhiteSpace($env:DEPENDENCY_TRACK_MODE)) { "full" } else { $env:DEPENDENCY_TRACK_MODE.Trim().ToLowerInvariant() }
if ($SkipDependencyTrack) {
    if ($reportingMode -eq "full" -and -not [string]::IsNullOrWhiteSpace($env:DEPENDENCY_TRACK_MODE)) {
        throw "-SkipDependencyTrack conflicts with DEPENDENCY_TRACK_MODE=full."
    }
    $reportingMode = "hosted-grype"
}
if ($reportingMode -notin @("full", "hosted-grype")) { throw "DEPENDENCY_TRACK_MODE must be 'full' or 'hosted-grype'." }
if ($reportingMode -eq "full") {
    if (-not (Add-ProcessEnvironmentArg -Name "DEPENDENCY_TRACK_API_KEY")) {
        throw "Full Dependency-Track reporting requires DEPENDENCY_TRACK_API_KEY in '$EnvFile' or the environment."
    }
    foreach ($name in "DEPENDENCY_TRACK_URL", "DEPENDENCY_TRACK_PROJECT_NAME", "DEPENDENCY_TRACK_SECURITY_GATE") {
        [void](Add-ProcessEnvironmentArg -Name $name)
    }
} else {
    Write-Step "DEPENDENCY_TRACK_MODE=hosted-grype: Dependency-Track upload and gate will be skipped." -Color DarkYellow
    $credentialArgs += @("-e", "DEPENDENCY_TRACK_API_KEY=")
}

$ownershipArgs = @()
if ($IsLinux) {
    $idCommand = Get-Command -Name "id" -CommandType Application -ErrorAction Stop | Select-Object -First 1
    $hostUid = (& $idCommand.Source -u | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $hostUid -notmatch '^[0-9]+$') { throw "Could not determine the Linux host UID." }
    $hostGid = (& $idCommand.Source -g | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $hostGid -notmatch '^[0-9]+$') { throw "Could not determine the Linux host GID." }
    $outputPath = $PSScriptRoot
    foreach ($segment in $outputRelative.Split('/')) {
        $outputPath = [System.IO.Path]::Combine($outputPath, $segment)
        $existing = Get-Item -LiteralPath $outputPath -Force -ErrorAction SilentlyContinue
        if ($existing) {
            if (($existing.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -or -not $existing.PSIsContainer) {
                throw "OutputDir must not contain links or non-directory entries: '$outputRelative'."
            }
        } else {
            $null = [System.IO.Directory]::CreateDirectory($outputPath)
        }
    }
    $ownershipArgs = @("-e", "HOST_UID=$hostUid", "-e", "HOST_GID=$hostGid")
}

Write-Step "Pulling $LabVIEWImage..."
docker pull $LabVIEWImage
if ($LASTEXITCODE -ne 0) { throw "docker pull $LabVIEWImage failed with exit code $LASTEXITCODE." }
$repository = $LabVIEWImage -replace '@.*$', '' -replace ':[^:/]+$', ''
$repoDigests = @(docker image inspect $LabVIEWImage --format '{{range .RepoDigests}}{{println .}}{{end}}' | Where-Object { $_ })
$labviewImageDigest = $repoDigests | Where-Object { $_ -like "$repository@*" } | Select-Object -First 1
if (-not $labviewImageDigest) { $labviewImageDigest = $repoDigests | Select-Object -First 1 }
if (-not $labviewImageDigest) { throw "Could not resolve the digest of $LabVIEWImage." }
Write-Step "Resolved $LabVIEWImage to $labviewImageDigest."

# Always rebuild without cache so OS updates and the base image are current
# (VIPM is pinned in the Dockerfile); building FROM the digest pins the image the SBOM describes.
Write-Step "Building $Image from $labviewImageDigest (no cache)..."
docker build --no-cache --build-arg "LABVIEW_IMAGE=$labviewImageDigest" -t $Image ([System.IO.Path]::Combine($PSScriptRoot, "docker", "linux-sbom"))
if ($LASTEXITCODE -ne 0) { throw "docker build failed with exit code $LASTEXITCODE." }

$workspaceArgs = @("-v", "${PSScriptRoot}:/workspace")
if ($IsLinux) {
    $workspacePath = $PSScriptRoot.Replace('"', '""')
    $workspaceArgs = @("--mount", "type=bind,`"source=$workspacePath`",target=/workspace,bind-recursive=disabled")
}
$runArgs = @(
    "run", "--rm"
) + $workspaceArgs + @(
    "-v", "sandbox-sbom-grype-db:/root/.cache/grype",
    "-e", "OUTPUT_DIR=$outputRelative",
    "-e", "LABVIEW_IMAGE=$LabVIEWImage",
    "-e", "LABVIEW_IMAGE_DIGEST=$labviewImageDigest",
    "-e", "DEPENDENCY_TRACK_MODE=$reportingMode",
    "-e", "INCLUDE_OS_DEPENDENCIES=$(if ($NiPackagesOnly) { '0' } else { '1' })",
    "-e", "SKIP_GRYPE=$(if ($SkipScan) { '1' } else { '0' })",
    "-e", "SBOM_SOURCE=$(if ($ManifestOnly) { 'manifest' } else { 'vipm' })"
) + $configArgs + $credentialArgs + $ownershipArgs + @($Image)

Write-Step "Running Linux SBOM pipeline for '$configRelative' in $Image..."
docker @runArgs
$exitCode = $LASTEXITCODE
if ($exitCode -eq 0) {
    Write-Step "Linux SBOM pipeline completed. Reports: $outputRelative" -Color Green
} else {
    Write-Step "Linux SBOM pipeline failed with exit code $exitCode." -Color Red
}
exit $exitCode
