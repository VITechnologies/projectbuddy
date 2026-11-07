# escape=\
# Ephemeral Linux LUnit release-gate image: National Instruments' own
# official LabVIEW Linux container, with the LUnit/LUnit CLI VI packages
# deployed by direct file copy (already SHA-256-verified at staging time
# by the shared Get-VerifiedLUnitPackageStaging.ps1 script, same as the
# Windows gate) -- no VIPM installation, no VIPM activation, no secrets,
# per ADR-0003/REQ-035/REQ-036.
#
# Unlike the Windows gate, this does NOT need an NSIS-built installer:
# Linux has no MAX_PATH limit and this image's container filesystem has
# no SMB-backed bind-mount equivalent, so the two real bugs that drove
# the Windows gate's NSIS migration (a MAX_PATH violation during
# extraction, an "access denied" Move-Item across a bind mount) do not
# apply here -- confirmed via direct local Docker testing on this
# engine's own host before being wired into CI (see
# docs/lunit-gate/procedure.md's Phase 2 section).
#
# Built and run by .github/workflows/lunit-gate-linux.yml; see
# docs/lunit-gate/procedure.md. Image pinned by digest (not a floating
# `latest-linux` tag) per ADR-0003's Integrity and pinning decision; this
# is the same 2026q1 release family as the Windows gate's pin, for
# consistency.
ARG LABVIEW_IMAGE=nationalinstruments/labview@sha256:597ba99260c0511475e27c04f13dd06f2f57e8ce7d3ee6c1ca289020b1f8c426
FROM ${LABVIEW_IMAGE}
USER root

COPY lunit-staging/LUnit /usr/local/natinst/LabVIEW-2026-64/vi.lib/Astemes/LUnit
COPY ["lunit-staging/LUnit CLI", "/usr/local/natinst/LabVIEW-2026-64/vi.lib/Astemes/LUnit CLI"]
COPY lunit-staging/dependency-manifest.json /lunit-gate/dependency-manifest.json
COPY invoke_lunit_tests_linux.py /lunit-gate/invoke_lunit_tests_linux.py

RUN test -f "/usr/local/natinst/LabVIEW-2026-64/vi.lib/Astemes/LUnit CLI/LUnitCLI/RunOperation.vi" \
    || (echo "LUnit deployment missing: RunOperation.vi" && exit 1)
RUN printf 'server.tcp.port=3363\nserver.tcp.access=+127.0.0.1\nserver.vi.access=+*\n' \
    >> /root/natinst/.config/LabVIEW-2026/labview.conf

LABEL org.opencontainers.image.title="projectbuddy LUnit Linux release gate"
LABEL org.opencontainers.image.description="Ephemeral, no-VIPM Linux LabVIEW container for ADR-0003's LUnit release gate (Phase 2)"
