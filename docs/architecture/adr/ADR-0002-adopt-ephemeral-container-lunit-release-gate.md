# ADR-0002: Adopt Ephemeral-Container LUnit Test Execution as a Release Gate

- Status: Accepted
- Date: 2026-11-07
- Deciders: svelderrainruiz (operator)

## Context

- This session's live-dispatch proof of `#62` (the Ubuntu-LabVIEW VM
  family's remaining `provision-ubuntu-vm.yml` phases) hit a real,
  compounding set of operational obstacles while trying to get a human
  operator into a freshly-provisioned VM's GUI to perform LabVIEW/VIPM
  Pro activation: a pre-existing host-health bug (`VBoxSVC` pegged near
  100% CPU for 5 days, ~1.3M context switches/sec, found and fixed this
  session), then a VirtualBox-GUI-under-Wayland input-grab limitation,
  then flakiness in VirtualBox's community VNC extension pack
  (`VBoxVNC`) causing repeated display renegotiation. None of this was
  specific to the Ubuntu family -- it is intrinsic to any workflow that
  requires a human to interactively drive a persistent VM's GUI for
  license activation.
- Operator direction during this troubleshooting: adopt
  `ni/labview-icon-editor#575` (open, not yet merged at the time of this
  decision) as the model instead. That PR adds a required LUnit job to
  `ni/labview-icon-editor`'s own CI: it builds a **fresh, ephemeral
  Windows Docker container** from `nationalinstruments/labview:2026q1-windows`
  on a GitHub-hosted `windows-2022` runner, deploys the open-source
  `astemes_lib_lunit`/`astemes_lib_lunit_cli` VI package files directly
  (checksum-verified, no VIPM installation -- VIPM installs are reported
  to stall in Windows containers), configures LabVIEW's TCP server, and
  invokes `LabVIEWCLI.exe -OperationName LUnit -LabVIEWPath ... -PortNumber 3363
  -AdditionalOperationDirectory <LUnit CLI dir>` to produce a JUnit
  `lunit.xml` report, which the workflow then validates (parses for
  failures, requires at least 10 non-skipped test cases). No VIPM
  activation secrets, license keys, or container-registry credentials
  are required anywhere in this flow, because the image is built fresh
  every run and never published or reused.
- This pattern is not foreign to this engine: `docker/windows-sbom/Dockerfile`
  and `docker/linux-sbom/Dockerfile` already build containers from NI's
  official `nationalinstruments/labview:latest-windows` and
  `nationalinstruments/labview:latest-linux` images for the containerized
  SBOM pipeline (see `docs/sbom-pipeline/procedure.md`). Extending that
  same base-image family to run LUnit tests is an incremental, not a
  novel, architectural move for this repo.
- `ni/labview-icon-editor#575` only establishes the Windows side; its own
  "Linux contract-test runner" references are about running the
  *PowerShell tooling scripts* cross-platform in CI, not about running
  LUnit itself against a Linux LabVIEW container. A Linux equivalent
  (mirroring the same ephemeral-container, no-activation architecture
  against `nationalinstruments/labview:latest-linux`) does not exist
  upstream yet and would be new work for this engine to originate.

## Decision

- Adopt **ephemeral-container LUnit test execution, requiring no
  persistent VIPM/LabVIEW activation**, modeled on
  `ni/labview-icon-editor#575`'s pattern, as a **release gate** for this
  engine going forward: a release must not ship unless both a Windows
  and a Linux ephemeral LabVIEW container can successfully execute a
  real LUnit test suite and produce a passing, validated JUnit report.
- This explicitly **replaces** persistent-VM GUI-activation (the
  `complete-activation` phase's human-interactive LabVIEW/VIPM licensing
  step) as the authoritative proof that this engine can run real LabVIEW
  automated tests, for the purposes of this gate. It does not retire the
  persistent-VM family itself (the Windows and Ubuntu LabVIEW VM
  families still serve the SBOM-generation and runner-registration use
  cases `#62` and its siblings were built for) -- it only means this
  specific capability (can we actually execute LabVIEW test code in CI)
  no longer depends on a human manually clicking through an activation
  wizard inside a VM, which this session showed is fragile VM-GUI
  infrastructure to depend on.
- Windows: directly port `ni/labview-icon-editor#575`'s concrete
  mechanism (checksum-verified LUnit/LUnit CLI VI package deployment,
  `LabVIEWCLI.exe -OperationName LUnit`, JUnit report validation)
  against this engine's own pinned `nationalinstruments/labview`
  Windows image.
- Linux: originate an equivalent flow against
  `nationalinstruments/labview:latest-linux` (this engine's own pinned
  version), reusing the same no-VIPM, checksum-verified LUnit package
  deployment and `LabVIEWCLI` invocation pattern, adapted for the Linux
  container's paths and CLI location.
- **Scope boundary for this decision**: this ADR records the
  architectural decision and the release-gate requirement. The actual
  implementation (the Windows and Linux container tooling, the
  GitHub Actions workflow(s), and a reusable "cookiecutter"-style
  template so consumer repos can adopt this gate without re-deriving it)
  is explicitly deferred to a dedicated post-release milestone -- see
  `#87` -- and does not block the release currently in flight
  (`v0.3.31` and its immediate successors). The gate takes effect once
  that milestone's implementation lands and is live-dispatch-proven, not
  retroactively.

## Rationale

- A human-interactive GUI-activation step inside a persistent VM is,
  empirically (this session), a fragile dependency: it failed for
  reasons layered three deep (host-health bug, Wayland input-grab
  limitation, VNC extension-pack flakiness) before ever reaching the
  actual LabVIEW activation dialog. A release gate should not depend on
  infrastructure this brittle.
- Ephemeral containers that need no persistent activation and no license
  secrets are strictly easier to run unattended, in CI, repeatedly, and
  without any human in the loop -- a stronger, more automatable proof of
  "this engine can actually run LabVIEW test code" than a one-time,
  manually-verified activation receipt.
- Running a well-known, real, external open-source project's own test
  suite (`ni/labview-icon-editor`'s LUnit tests) is a more rigorous and
  externally-legible proof than this engine's own synthetic
  known-answer probe (`capture-activation.sh`'s current approach) --
  it demonstrates compatibility with a real, independently-maintained
  LabVIEW codebase, not just a self-authored check.
- This session's host-health finding (`VBoxSVC` pegged for 5 days) was
  an unrelated, pre-existing problem this troubleshooting happened to
  surface and fix -- a concrete example of why persistent, long-lived
  VM/VirtualBox infrastructure accumulates this kind of operational risk
  over time in a way ephemeral, rebuilt-every-run containers do not.

## Consequences

- Positive:
  - A release gate that can run fully unattended in CI, with no human
    GUI interaction and no license secrets beyond what's already
    required for the existing containerized SBOM pipeline's VIPM
    activation (which uses the same no-GUI `vipm activate
    --direct-vipm-io` pattern already, per `docs/sbom-pipeline/procedure.md`).
  - Reuses this engine's existing `nationalinstruments/labview` container
    base-image investment rather than introducing a new one.
  - A stronger, externally-verifiable compatibility proof than the
    current synthetic activation probe.
- Negative:
  - The Linux side is net-new work this engine must originate and
    maintain itself (no upstream reference implementation exists yet),
    carrying real implementation and maintenance cost.
  - Introduces a new release-blocking gate; a release cannot ship if
    either platform's LUnit execution regresses, even if every other
    gate passes -- a real increase in release friction, accepted here as
    the cost of a materially stronger compatibility guarantee.
  - Does not eliminate the persistent-VM family or its own activation
    workflow (`complete-activation`) -- that infrastructure is still
    maintained for its own (SBOM/runner-registration) use cases, so this
    decision adds a second, parallel LabVIEW-execution path rather than
    replacing the VM family outright.
