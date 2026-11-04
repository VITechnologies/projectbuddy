# Activates VIPM Pro from environment variables inside NI's LabVIEW Windows
# container, then runs the requested command (default: the SBOM pipeline).
$ErrorActionPreference = "Stop"

& C:\sbom\Test-VipmVersion.ps1 -CliVersion $env:VIPM_EXPECTED_CLI_VERSION -DesktopVersion $env:VIPM_EXPECTED_DESKTOP_VERSION

if ($env:SBOM_SOURCE -ne "manifest") {
    foreach ($name in "VIPM_SERIAL_NUMBER", "VIPM_FULL_NAME", "VIPM_EMAIL") {
        if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name))) {
            [Console]::Error.WriteLine("[ERROR] $name is not set. VIPM Pro activation is required for 'vipm sbom'.")
            exit 1
        }
    }
    Write-Host "[INFO] Activating VIPM Pro..."
    # --direct-vipm-io activates without VIPM Desktop, whose first-run dialogs
    # cannot be answered in a container.
    $activation = & vipm activate --direct-vipm-io --serial-number $env:VIPM_SERIAL_NUMBER `
        --name $env:VIPM_FULL_NAME --email $env:VIPM_EMAIL 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) {
        [Console]::Error.WriteLine($activation.Replace($env:VIPM_SERIAL_NUMBER, "***"))
        [Console]::Error.WriteLine("[ERROR] VIPM Pro activation failed with exit code $LASTEXITCODE.")
        exit 1
    }
}
foreach ($name in "VIPM_SERIAL_NUMBER", "VIPM_FULL_NAME", "VIPM_EMAIL") {
    Remove-Item -Path "Env:$name" -ErrorAction SilentlyContinue
}

. C:\sbom\Initialize-VipmContainer.ps1
if ($env:SBOM_SOURCE -ne "manifest") {
    Initialize-VipmContainer
}

$invocation = Resolve-VipmContainerCommandLine -Arguments $args -DefaultCommand "C:\sbom\windows_sbom_pipeline.ps1"
$commandArguments = @($invocation.Arguments)
& $invocation.Command @commandArguments
exit $LASTEXITCODE
