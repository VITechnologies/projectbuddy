# escape=`
# Ephemeral Windows LUnit release-gate image: National Instruments' own
# official LabVIEW Windows container, with the LUnit/LUnit CLI VI packages
# deployed by direct, SHA-256-verified file copy -- no VIPM installation,
# no VIPM activation, no secrets, per ADR-0003/REQ-034.
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
COPY Install-LUnitPackages.ps1 C:/lunit-gate/Install-LUnitPackages.ps1

RUN & C:\lunit-gate\Install-LUnitPackages.ps1 `
        -LabviewRoot 'C:\Program Files\National Instruments\LabVIEW 2026' `
        -ResultsDirectory 'C:\lunit-gate\setup-results'; `
    if (-not (Test-Path C:\lunit-gate\setup-results\dependency-manifest.json)) { throw 'Required LUnit dependency manifest is missing.' }; `
    Copy-Item C:\lunit-gate\setup-results\dependency-manifest.json C:\lunit-gate\dependency-manifest.json; `
    Remove-Item C:\lunit-gate\setup-results -Recurse -Force

LABEL org.opencontainers.image.title="projectbuddy LUnit Windows release gate"
LABEL org.opencontainers.image.description="Ephemeral, no-VIPM Windows LabVIEW container for ADR-0003's LUnit release gate"
