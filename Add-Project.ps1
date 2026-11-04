<#
.SYNOPSIS
    Dispatches the Add Project GitHub Actions workflow using local configuration files.
#>

param(
    [Parameter(Mandatory = $true)]
    [string]$PipelineConfigPath,

    [Parameter(Mandatory = $true)]
    [string]$VipmTomlPath,

    [string]$Labels = "self-hosted,windows",

    [switch]$ValidateOnly,

    [string]$Ref
)

$ErrorActionPreference = "Stop"

foreach ($path in @($PipelineConfigPath, $VipmTomlPath)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Input file not found: $path"
    }
}

$pipelineConfig = Get-Content -LiteralPath $PipelineConfigPath -Raw -Encoding utf8
$vipmToml = Get-Content -LiteralPath $VipmTomlPath -Raw -Encoding utf8

try {
    $config = $pipelineConfig | ConvertFrom-Json
} catch {
    throw "Pipeline configuration file must contain valid JSON: $($_.Exception.Message)"
}

if ($null -eq $config -or $null -eq $config.projects -or @($config.projects).Count -ne 1) {
    throw "Pipeline configuration must contain exactly one project in its projects array."
}
try {
    $schemaPath = Join-Path $PSScriptRoot "pipeline-config.schema.json"
    $schemaValid = Test-Json -Json $pipelineConfig -SchemaFile $schemaPath -ErrorAction Stop
} catch {
    throw "Pipeline configuration must satisfy pipeline-config.schema.json: $($_.Exception.Message)"
}
if (-not $schemaValid) {
    throw "Pipeline configuration must satisfy pipeline-config.schema.json."
}
$normalizedLabels = @($Labels -split "," | ForEach-Object { $_.Trim() } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
if ($normalizedLabels.Count -eq 0) {
    throw "Labels must contain at least one runner label."
}
$Labels = $normalizedLabels -join ","
if ([string]::IsNullOrWhiteSpace($vipmToml)) {
    throw "VIPM TOML file must not be blank."
}

if (-not ($python = Get-Command -Name "python3" -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1)) {
    $python = Get-Command -Name "python" -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
}
if (-not $python) {
    throw "Python 3.11 or later is required to validate the VIPM TOML file."
}
$null = & $python.Source -c "import sys, tomllib" 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "Python 3.11 or later is required to validate the VIPM TOML file."
}
$runtimeValidator = Join-Path $PSScriptRoot ".github/scripts/project_runtime.py"
$registrationValidator = Join-Path $PSScriptRoot ".github/scripts/validate-project-registration.py"
$credentialErrors = $pipelineConfig | & $python.Source $registrationValidator --validate-credentials 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "Pipeline configuration contains an invalid credential reference: $($credentialErrors -join ' ')"
}
$runtimeErrors = $pipelineConfig | & $python.Source $runtimeValidator --validate 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "Pipeline runtime support is invalid: $($runtimeErrors -join ' ')"
}
$tomlParser = @"
import sys, tomllib
try:
    tomllib.load(sys.stdin.buffer)
except tomllib.TOMLDecodeError as error:
    print(error, file=sys.stderr)
    sys.exit(1)
"@
$tomlError = [System.IO.File]::ReadAllBytes($VipmTomlPath) | & $python.Source -c $tomlParser 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "VIPM TOML file is invalid: $($tomlError -join ' ')"
}

if ($ValidateOnly) {
    Write-Output "Pipeline configuration, runtime support, and VIPM TOML passed local validation."
    return
}

if ([string]::IsNullOrWhiteSpace($Ref)) {
    $Ref = & git -C $PSScriptRoot branch --show-current
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($Ref)) {
        throw "Could not determine the current Git branch. Specify -Ref explicitly."
    }
}

$payload = @{
    pipeline_config = $pipelineConfig
    labels          = $Labels
    vipm_toml       = $vipmToml
} | ConvertTo-Json -Compress

Push-Location $PSScriptRoot
try {
    $payload | & gh workflow run add-project.yml --ref $Ref --json
    if ($LASTEXITCODE -ne 0) {
        throw "GitHub CLI failed to dispatch add-project.yml. Ensure gh is installed and authenticated."
    }
} finally {
    Pop-Location
}
