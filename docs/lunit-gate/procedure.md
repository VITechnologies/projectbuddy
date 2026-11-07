# LUnit Release Gate Procedure (ADR-0003, Phase 1: Windows)

- Status: Active (Phase 1 implementation; dual-platform gate not yet in
  effect -- see `REQ-033`)
- Last substantive update: 2026-11-07
- Owner: svelderrainruiz

Implements `docs/architecture/adr/ADR-0003-ephemeral-container-lunit-release-gate-revised.md`'s
Phase 1 (`REQ-034`): an ephemeral, no-VIPM, GitHub-hosted-runner Windows
LabVIEW container that runs a pinned commit of `ni/labview-icon-editor`'s
real LUnit test suite, proving this engine can execute genuine, unattended
LabVIEW automated tests without depending on persistent-VM GUI activation.
This is separate from, and does not retire, `docker/windows-sbom/`'s own
containerized SBOM pipeline (which does install/activate VIPM for its own,
different purpose) or the Windows/Ubuntu LabVIEW VM families (`#62`).

## What this proves, and what it does not

- **Proves**: this engine's pinned `nationalinstruments/labview` Windows
  image, with the LUnit/LUnit CLI VI packages deployed by direct,
  SHA-256-verified file copy (no VIPM), can run a real, independently-
  maintained LabVIEW project's test suite unattended via native
  `LabVIEWCLI`, and produce a validated JUnit report -- live-dispatch-
  proven via a real `workflow_dispatch` run, not merely a code review.
- **Does not prove**: anything about the Linux family (`REQ-035`/`REQ-036`,
  Phase 2, not yet attempted), or `#62`'s persistent-VM
  `complete-activation`/`package-build` phases (reopened, separately
  tracked, unrelated to this gate).

## Components

| File | Responsibility |
| --- | --- |
| `docker/lunit-gate/windows.Dockerfile` | Builds the ephemeral image from a digest-pinned `nationalinstruments/labview` Windows base, baking in SHA-256-verified LUnit packages at build time (no VIPM). |
| `docker/lunit-gate/Install-LUnitPackages.ps1` | The Dockerfile's `RUN` step: downloads, SHA-256-verifies, and deploys `astemes_lib_lunit`/`astemes_lib_lunit_cli` directly into `vi.lib\Astemes\`; writes `dependency-manifest.json` as evidence. `Confirm-PackageIntegrity` is the pure, offline-testable verification function (`scripts/tests/Install-LUnitPackages.Tests.ps1`). |
| `docker/lunit-gate/Invoke-LUnitTests.ps1` | Clones the pinned `ni/labview-icon-editor` commit, starts LabVIEW headless, runs `LabVIEWCLI -OperationName LUnit`, and validates the resulting `lunit.xml` against the named-waiver-only acceptance policy. `Test-JUnitReportAcceptable` is the pure, offline-testable policy function (`scripts/tests/Invoke-LUnitTests.Tests.ps1`). |
| `docker/lunit-gate/Build-LUnitWindowsImage.ps1` | Host-side image builder: stages only the Dockerfile and setup script into a clean build context (never the full worktree). |
| `.github/workflows/lunit-gate-windows.yml` | `workflow_dispatch`, `runs-on: windows-2022` (GitHub-hosted, per `ADR-0003`'s explicit runner-class decision) -- builds the image, runs the container, validates the dependency manifest has no VIPM, runs the gate, uploads results. |

## Package provenance (SHA-256 pinning)

Per `ADR-0003`'s Integrity and pinning decision (SHA-256, not the upstream
reference's MD5): the pinned hashes in `Install-LUnitPackages.ps1` were
obtained by downloading `astemes_lib_lunit-2.0.13.6.vip` and
`astemes_lib_lunit_cli-1.7.1.32.vip` from JKI's public package server,
cross-checking each file's MD5 against the value published in
`ni/labview-icon-editor#575`'s own reference script (confirming
authenticity against the same source that script already trusts), then
computing and pinning this engine's own SHA-256 of that same,
MD5-verified file. Refreshing these pins (a version bump) requires
repeating this same two-step verification, not just updating the version
string.

## Suite commit pinning

`lunit-gate-windows.yml`'s `suite_commit` input defaults to
`d9eac771ccac0ed92bde81a031fe2f4ebe62c845` (`ni/labview-icon-editor`'s
`main` branch HEAD at the time Phase 1 was implemented, confirmed to
contain `lv_icon_editor.lvproj` and 5 real `Test/Unit Tests/` classes).
Bumping this pin is a deliberate operator decision (dispatch with a new
`suite_commit` value), not an automatic drift -- per this engine's
existing upgrade-path convention (`README.md`'s "Upgrade path" bullet,
`REQ-003`).

## The named-waiver policy

Per `ADR-0003`'s round-2 Addendum: the **only** test case permitted to
fail or error without blocking the gate is `Test Write Read Library Icon
Data.vi` (a known, disclosed upstream `NI.Lib.VIIconTemplate`/
`NI_IconEditor` tag-mismatch, per `ni/labview-icon-editor#575`'s own
description). Any other test's failure or error blocks the gate. Waiving
a different test requires a new superseding ADR, not an edit to
`Invoke-LUnitTests.ps1`'s `$script:AllowedWaiverTestName`.

## Running it

```
gh workflow run lunit-gate-windows.yml --repo VITechnologies/projectbuddy --ref main
```

Optionally pass `-f suite_commit=<sha>` to test a different pinned
upstream commit. Results (including `lunit.xml`, `gate-result.json`, and
`dependency-manifest.json`) are uploaded as the
`lunit-gate-windows-results-<run-id>` artifact.

## Related

- `docs/architecture/adr/ADR-0003-ephemeral-container-lunit-release-gate-revised.md`
- `docs/architecture/adr/ADR-0002-adopt-ephemeral-container-lunit-release-gate.md` (superseded)
- Wiki `Software-Requirements-Specification` `REQ-033`-`REQ-036`
- `#87` (this implementation's tracking issue)
- `#62` (reopened; unrelated to this gate)
