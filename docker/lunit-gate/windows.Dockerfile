# escape=`
# Ephemeral Windows LUnit release-gate image: National Instruments' own
# official LabVIEW Windows container, with the LUnit/LUnit CLI VI packages
# deployed by an NSIS-built installer (Install-LUnitPackages.nsi, compiled
# by Build-LUnitWindowsImage.ps1 from already SHA-256-verified staged
# files) -- no VIPM installation, no VIPM activation, no secrets, per
# ADR-0003/REQ-034. NSIS's own File/CreateDirectory instructions replaced
# an earlier raw PowerShell + .NET ZipFile approach after two real
# Windows-container-specific failures: a MAX_PATH violation during
# ZipFile.ExtractToDirectory, and an "access denied" failure moving a
# directory across this container's SMB-backed bind mount. Validated
# end-to-end (real LabVIEW + LabVIEWCLI + the pinned suite) against a
# local Vagrant harness (docker/lunit-gate/vagrant-nsis-test/) before
# being wired in here -- see docs/lunit-gate/procedure.md.
#
# Built and run by .github/workflows/lunit-gate-windows.yml; see
# docs/lunit-gate/procedure.md. Image pinned by digest (not a floating
# `latest-*` tag) per ADR-0003's Integrity and pinning decision; this is
# ni/labview-icon-editor#575's own reference tag
# (nationalinstruments/labview:2026q1-windows), resolved to its digest for
# this engine's own release-gate pin.
ARG LABVIEW_IMAGE=nationalinstruments/labview@sha256:57c453dabd2ff0185ce718d88704921bb82eb83189f4049205ed9b4da7df7bcd
FROM ${LABVIEW_IMAGE}
USER ContainerAdministrator

SHELL ["powershell", "-NoProfile", "-Command", "$ErrorActionPreference = 'Stop'; $ProgressPreference = 'SilentlyContinue';"]
COPY Install-LUnitPackages.exe C:/lunit-gate/Install-LUnitPackages.exe

# Start-Process -Wait -PassThru, not the `&` call operator: PowerShell's `&`
# does not reliably wait for/capture the exit code of an NSIS-compiled
# (GUI-subsystem) executable (confirmed gotcha, nsis-host-installers skill).
RUN $p = Start-Process -FilePath 'C:\lunit-gate\Install-LUnitPackages.exe' -ArgumentList '/S' -Wait -PassThru; `
    if ($p.ExitCode -ne 0) { `
        if (Test-Path 'C:\lunit-gate\install.log') { Get-Content 'C:\lunit-gate\install.log' | Write-Host }; `
        throw "LUnit package installer exited $($p.ExitCode)."; `
    }; `
    if (-not (Test-Path 'C:\lunit-gate\install.log')) { throw 'LUnit package installer produced no log -- it did not run.' }; `
    $log = Get-Content -Raw 'C:\lunit-gate\install.log'; `
    if ($log -notmatch 'completed successfully') { Write-Host $log; throw 'LUnit package installer did not report success; see log above.' }; `
    if (-not (Test-Path 'C:\lunit-gate\dependency-manifest.json')) { throw 'Required LUnit dependency manifest is missing.' }

LABEL org.opencontainers.image.title="projectbuddy LUnit Windows release gate"
LABEL org.opencontainers.image.description="Ephemeral, no-VIPM Windows LabVIEW container for ADR-0003's LUnit release gate"


