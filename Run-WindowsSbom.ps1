<#
.SYNOPSIS
    Generates a Windows SBOM for a LabVIEW project inside a Windows Docker container.

.DESCRIPTION
    Builds docker/windows-sbom (NI's official LabVIEW Windows image + VIPM CLI,
    PowerShell 7, Git, Subversion and Grype) with the local Docker engine, for
    example Docker Desktop in Windows containers mode, then runs the container
    pipeline against this repository mounted at C:\workspace. No project is
    selected by default -- see -ConfigName/-VipmTomlPath below.

    Every run pulls -LabVIEWImage (default nationalinstruments/labview:latest-windows),
    resolves it to a digest, and rebuilds the image without cache from that
    digest. The SBOM records both the tag and the digest.

    VIPM Pro activation is required. Provide VIPM_SERIAL_NUMBER, VIPM_FULL_NAME
    and VIPM_EMAIL as process environment variables or in the -EnvFile file
    (default: docker/windows-sbom/.env, ignored by Git). Values in the file
    take precedence over process environment variables.

    SVN_GLOBAL_USER and SVN_GLOBAL_PASSWORD, from the env file or the process
    environment, are passed to the container and used for SVN projects whose
    configuration supplies no username and password or token.

    DEPENDENCY_TRACK_MODE=full (default) requires the API key, reachable backend,
    and fresh Critical/High metrics. hosted-grype explicitly skips the upload.

    The run also fails when Grype reports findings at or above GRYPE_FAIL_ON
    (default: high; "none" reports only).

    Reports are written to output/windows/<project-name>/.

.EXAMPLE
    .\Run-WindowsSbom.ps1

.EXAMPLE
    .\Run-WindowsSbom.ps1 -SkipScan -SkipDependencyTrack

.EXAMPLE
    # Hosts that cannot run the image with process isolation.
    .\Run-WindowsSbom.ps1 -Isolation hyperv -Memory 8g

.EXAMPLE
    # Without VIPM Pro: base SBOM from vipm.toml plus the nipkg deep scan.
    .\Run-WindowsSbom.ps1 -ManifestOnly
#>
[CmdletBinding()]
param(
    # No built-in default -- this is a generic engine shared by any fork;
    # SandboxSBOMReporting's own real project is only ever supplied via
    # PROJECTS_OVERRIDE (CI) or an explicit -ConfigName/-VipmTomlPath
    # (documented local invocation example), never hardcoded here.
    [string]$ConfigName = "",
    [string]$VipmTomlPath = "",
    [string]$EnvFile = "docker/windows-sbom/.env",
    [string]$Image = "sandbox-sbom-windows:latest",
    [string]$LabVIEWImage = "nationalinstruments/labview:latest-windows",
    [string]$OutputDir = "output/windows",
    [ValidateSet("process", "hyperv")]
    [string]$Isolation = "process",
    [string]$Memory = "8g",
    [switch]$SkipScan,
    [switch]$ManifestOnly,
    [switch]$SkipDependencyTrack,
    # Root used to resolve project-specific paths (-ConfigName/-VipmTomlPath/
    # -EnvFile/-OutputDir, and the container's C:\workspace mount) -- distinct
    # from $PSScriptRoot, which stays the location of this generic engine
    # script itself (pipeline-config.schema.json, docker/windows-sbom/) and
    # is separately mounted at C:\engine so the in-container pipeline can
    # dot-source Build-Sbom.ps1 and read its own engine-root files even when
    # $WorkspaceRoot is a different, real consumer repository. Defaults to
    # $PSScriptRoot so existing invocations are unaffected; a consumer
    # invoking this script from a pinned external checkout (see
    # README.md's "Consumption model" section) must pass the real repo root here.
    [string]$WorkspaceRoot = $PSScriptRoot
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message, [string]$Color = "Cyan")
    Write-Host "[$((Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))] $Message" -ForegroundColor $Color
}

function ConvertTo-RepoRelativePath {
    param([string]$Path, [string]$Name)
    $full = [System.IO.Path]::GetFullPath([System.IO.Path]::Combine($WorkspaceRoot, $Path))
    $root = [System.IO.Path]::GetFullPath($WorkspaceRoot).TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    if (-not $full.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Name must be inside the repository: '$Path'."
    }
    return $full.Substring($root.Length).Replace('\', '/')
}

if (-not $IsWindows) {
    throw "Run-WindowsSbom.ps1 requires a Windows host with Docker in Windows containers mode. Use Run-LinuxSbom.ps1 for Linux containers."
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker CLI not found. Install and start Docker Desktop."
}
$engineOs = docker info --format '{{.OSType}}' 2>$null
if ($LASTEXITCODE -ne 0) { throw "Docker engine is not reachable. Start Docker Desktop." }
if ($engineOs -ne "windows") {
    throw "Docker engine OS is '$engineOs'. Switch Docker Desktop to Windows containers."
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
    $configText = [System.IO.File]::ReadAllText([System.IO.Path]::Combine($WorkspaceRoot, $configRelative))
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
    if (-not (Test-Path -LiteralPath ([System.IO.Path]::Combine($WorkspaceRoot, $tomlRelative)) -PathType Leaf)) {
        throw "VIPM manifest '$tomlRelative' does not exist."
    }
    $configArgs += @("-e", "VIPM_TOML=$tomlRelative")
} else {
    $configArgs += @("-e", "VIPM_TOML_CONFIGS")
}

$credentialArgs = @()
$envFilePath = [System.IO.Path]::Combine($WorkspaceRoot, $EnvFile)
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
            throw "Set $name or add it to '$EnvFile' (see docker/windows-sbom/.env.example). VIPM Pro is required for 'vipm sbom'; use -ManifestOnly to run without it."
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

Write-Step "Pulling $LabVIEWImage..."
docker pull $LabVIEWImage
if ($LASTEXITCODE -ne 0) { throw "docker pull $LabVIEWImage failed with exit code $LASTEXITCODE." }
$repository = $LabVIEWImage -replace '@.*$', '' -replace ':[^:/]+$', ''
$repoDigests = @(docker image inspect $LabVIEWImage --format '{{range .RepoDigests}}{{println .}}{{end}}' | Where-Object { $_ })
$labviewImageDigest = $repoDigests | Where-Object { $_ -like "$repository@*" } | Select-Object -First 1
if (-not $labviewImageDigest) { $labviewImageDigest = $repoDigests | Select-Object -First 1 }
if (-not $labviewImageDigest) { throw "Could not resolve the digest of $LabVIEWImage." }
Write-Step "Resolved $LabVIEWImage to $labviewImageDigest."

$isolationArgs = @("--isolation=$Isolation")
if ($Isolation -eq "hyperv") { $isolationArgs += @("--memory", $Memory) }

# Always rebuild without cache so the base image is current (VIPM is pinned
# in the Dockerfile); building FROM the digest pins the image the SBOM describes.
Write-Step "Building $Image from $labviewImageDigest (no cache, $Isolation isolation)..."
docker build --no-cache @isolationArgs --build-arg "LABVIEW_IMAGE=$labviewImageDigest" -t $Image ([System.IO.Path]::Combine($PSScriptRoot, "docker", "windows-sbom"))
if ($LASTEXITCODE -ne 0) { throw "docker build failed with exit code $LASTEXITCODE." }

$runArgs = @(
    "run", "--rm"
) + $isolationArgs + @(
    "-v", "${WorkspaceRoot}:C:\workspace",
    "-v", "${PSScriptRoot}:C:\engine",
    "-v", "sandbox-sbom-grype-db-windows:C:\grype-cache",
    "-e", "OUTPUT_DIR=$outputRelative",
    "-e", "LABVIEW_IMAGE=$LabVIEWImage",
    "-e", "LABVIEW_IMAGE_DIGEST=$labviewImageDigest",
    "-e", "DEPENDENCY_TRACK_MODE=$reportingMode",
    "-e", "SKIP_GRYPE=$(if ($SkipScan) { '1' } else { '0' })",
    "-e", "SBOM_SOURCE=$(if ($ManifestOnly) { 'manifest' } else { 'vipm' })"
) + $configArgs + $credentialArgs + @($Image)

Write-Step "Running Windows SBOM pipeline for '$configRelative' in $Image..."
docker @runArgs
$exitCode = $LASTEXITCODE
if ($exitCode -eq 0) {
    Write-Step "Windows SBOM pipeline completed. Reports: $outputRelative" -Color Green
} else {
    Write-Step "Windows SBOM pipeline failed with exit code $exitCode." -Color Red
}
exit $exitCode
