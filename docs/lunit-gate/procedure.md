# LUnit Release Gate Procedure (ADR-0003: Windows + Linux)

- Status: Active (Phase 1 and Phase 2 both implemented and live-dispatch-
  proven on fork and upstream; the dual-platform gate is now in effect
  per `REQ-033`, promoted to Active 2026-11-07)
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
  and Linux images, with the LUnit/LUnit CLI VI packages deployed via an
  NSIS-built installer (Windows) or a plain file copy (Linux -- no
  NSIS-equivalent needed, see Phase 2 below), can each run a real,
  independently-maintained LabVIEW project's test suite unattended via
  native `LabVIEWCLI`, and produce a validated JUnit report --
  live-dispatch-proven via real `workflow_dispatch` runs on both the
  fork and upstream `VITechnologies/projectbuddy`, not merely a code
  review or local-only test. Windows was also validated end-to-end
  against a real LabVIEW installation via a local Vagrant harness
  (`docker/lunit-gate/vagrant-nsis-test/`) before ever being wired into
  CI; Linux (which runs natively on this build host, unlike Windows
  containers) was validated the same way via direct local Docker
  testing instead.
- **Does not prove**: anything about `#62`'s persistent-VM
  `complete-activation`/`package-build` phases (reopened, separately
  tracked, unrelated to this gate).

## Why NSIS, not raw PowerShell + .NET ZipFile

The first implementation attempt deployed packages via `ZipFile.ExtractToDirectory`
and moved the downloaded test suite with `Move-Item`, both called directly
from PowerShell inside the container. Two real, Windows-container-specific
failures surfaced on live `workflow_dispatch` runs before this was caught:

1. A Windows `MAX_PATH` (260-char) violation during `ZipFile.ExtractToDirectory`,
   triggered by a long upstream filename nested under a temp-extraction
   path that repeated `$Repo-$Commit` twice.
2. An "Access to the path ... is denied" failure from `Move-Item`, caused
   by moving a directory across the container's SMB-backed bind mount
   (`C:\workspace`) -- Windows-container bind mounts behave differently
   from local NTFS for rename/move operations.

Both are exactly the kind of Windows-deployment edge case NSIS (decades of
production use placing files into Windows system directories) is built to
avoid. Package deployment now uses `Install-LUnitPackages.nsi`'s own
`File`/`CreateDirectory` instructions instead, with the package files
already SHA-256-verified and staged *before* compilation (so no network
access or container-specific quirks are needed inside the container at
deployment time at all). The suite-download step (an external GitHub
repository, not a system-directory install) still uses a plain PowerShell
archive download -- that part was never affected by either bug once moved
outside the bind mount (see `Invoke-LUnitTests.ps1`'s own comments).

## Components

| File | Responsibility |
| --- | --- |
| `docker/lunit-gate/windows.Dockerfile` | Builds the ephemeral image from a digest-pinned `nationalinstruments/labview` Windows base, running the pre-compiled NSIS installer at build time (no VIPM). Uses `Start-Process -Wait -PassThru` to invoke it, not `&` (PowerShell's call operator does not reliably capture an NSIS GUI-subsystem exe's exit code -- see the `nsis-host-installers` skill). |
| `docker/lunit-gate/Get-VerifiedLUnitPackageStaging.ps1` | **Host-side** (runs before `docker build`, on the GitHub Actions runner itself, not inside the container): downloads, SHA-256-verifies, and extracts `astemes_lib_lunit`/`astemes_lib_lunit_cli` into a flat staging directory ready for NSIS to bundle. `Confirm-PackageIntegrity` is the pure, offline-testable verification function (`scripts/tests/Get-VerifiedLUnitPackageStaging.Tests.ps1`). |
| `docker/lunit-gate/Install-LUnitPackages.nsi` | The NSIS installer source: bundles the staged, already-verified package files via `File /r` and extracts them directly to `$INSTDIR\vi.lib\Astemes\...` at install time, configures `LabVIEW.ini`'s TCP VI Server settings (required for `LabVIEWCLI`), and writes a persistent, structural-determinism log (`C:\lunit-gate\install.log`) via the same `RunStep` macro pattern as `vm/windows-labview/winrm-setup-installer.nsi`. Compiled by `Build-LUnitWindowsImage.ps1` via `makensis` (pre-installed on `windows-2022` GitHub-hosted runners; also installable locally via `apt install nsis` for fast iteration -- no Wine or Windows build environment needed to *compile*, only to *run* the result). |
| `docker/lunit-gate/Invoke-LUnitTests.ps1` | Downloads the pinned `ni/labview-icon-editor` commit as a GitHub archive (no `git` dependency -- the NI base image doesn't include it; an exact-SHA archive is inherently that commit, no separate verification needed), starts LabVIEW headless, runs `LabVIEWCLI -OperationName LUnit`, and validates the resulting `lunit.xml` against the named-waiver-only acceptance policy. `Test-JUnitReportAcceptable` is the pure, offline-testable policy function (`scripts/tests/Invoke-LUnitTests.Tests.ps1`). |
| `docker/lunit-gate/Build-LUnitWindowsImage.ps1` | Host-side orchestrator: runs the staging step, compiles the NSIS installer with the verified files bundled in, then builds the Docker image from a clean context (never the full worktree). |
| `docker/lunit-gate/vagrant-nsis-test/` | **Fast local iteration harness** (not part of the CI pipeline): mirrors `vm/windows-labview/vagrant-nsis-test/`'s established pattern exactly, reusing the same `actor/win11-labview2026` golden box, to validate the NSIS installer (and, ad hoc, the full LUnit execution chain) against a real Windows+LabVIEW environment in minutes instead of the ~15-20 minute GitHub Actions Docker-build cycle for every iteration. |
| `.github/workflows/lunit-gate-windows.yml` | `workflow_dispatch`, `runs-on: windows-2022` (GitHub-hosted, per `ADR-0003`'s explicit runner-class decision) -- builds the image, runs the container, validates the dependency manifest has no VIPM, runs the gate, uploads results. |

## Package provenance (SHA-256 pinning)

Per `ADR-0003`'s Integrity and pinning decision (SHA-256, not the upstream
reference's MD5): the pinned hashes in `Get-VerifiedLUnitPackageStaging.ps1`
were obtained by downloading `astemes_lib_lunit-2.0.13.6.vip` and
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

## Fast local iteration (before touching CI)

For changes to `Install-LUnitPackages.nsi` or `Invoke-LUnitTests.ps1`,
iterate locally against a real Windows+LabVIEW environment instead of a
~15-20 minute GitHub Actions Docker-build cycle per attempt:

```
# Compile locally (no Windows machine needed to compile, only to run):
pwsh -NoProfile -File docker/lunit-gate/Get-VerifiedLUnitPackageStaging.ps1 -StagingDirectory /tmp/lunit-staging
makensis -DLUNIT_STAGING=/tmp/lunit-staging docker/lunit-gate/Install-LUnitPackages.nsi
mv Install-LUnitPackages.exe docker/lunit-gate/

cd docker/lunit-gate/vagrant-nsis-test
vagrant up
vagrant provision --provision-with run-installer
vagrant winrm -c 'Get-Content C:\lunit-gate\install.log' -e
vagrant destroy -f   # when done -- this is a disposable test VM
```

The box (`actor/win11-labview2026`, a private maintainer-held box per
`vm/windows-labview/vagrant-nsis-test/Vagrantfile`'s own documentation)
has VIPM pre-installed for unrelated purposes -- `Invoke-LUnitTests.ps1`'s
no-VIPM-presence check will correctly reject it if run there directly;
dot-source the script and call its functions individually (bypassing only
that one guard) to validate the suite-download/LabVIEWCLI mechanics
against this box instead.

## Phase 2: Linux (`REQ-035`/`REQ-036`)

Implements the Linux feasibility spike and execution requirements. Unlike
Windows, Linux containers run **natively** on this engine's own Linux
build host, so iteration here is direct local Docker testing rather than
a separate VM/Vagrant harness.

### Findings

- **Feasible, confirmed.** `LabVIEWCLI`'s `LUnit` operation runs to
  completion against the pinned Linux `nationalinstruments/labview`
  image with **no `Xvfb`/`DISPLAY` required at all** -- LabVIEW's VI
  Server becomes ready in seconds with no virtual framebuffer. This
  resolves the headless-display risk `ADR-0003`'s original Decision
  raised as open.
- **No NSIS-equivalent installer needed.** Neither Windows-container bug
  that drove Phase 1's NSIS migration (`MAX_PATH`, the SMB-backed bind
  mount's `Move-Item` failure) applies to Linux; `linux.Dockerfile`
  deploys the staged, SHA-256-verified package files with a plain
  `COPY`.
- **Three named waivers, not one.** Beyond the Windows-shared
  `Test Write Read Library Icon Data.vi`, two Linux-specific test
  failures were found and confirmed via direct inspection of each
  failure's exact assertion diff (not assumption): `Test Window
  Position.vi` (every window-bounds assertion off by a consistent
  1-pixel offset -- a Linux-window-manager-vs-Windows rendering
  difference) and `Test INI Settings.vi` (expects the Windows-only
  default font `"Small Fonts"`; Linux LabVIEW's real default is
  `"LabVIEW Application"`). Both are platform differences in the test's
  hardcoded expectations, not functional defects in this engine's
  deployment. See `ADR-0003`'s Phase-2 Addendum and the wiki SRS's
  `REQ-036` for the full reasoning.

### Components

| File | Responsibility |
| --- | --- |
| `docker/lunit-gate/linux.Dockerfile` | Builds the ephemeral image from a digest-pinned `nationalinstruments/labview` Linux base. Uses the JSON-array `COPY ["LUnit CLI", ...]` form for the space-containing package directory name (a plain backslash-escaped `COPY` does not parse the same way as shell escaping in a Linux Dockerfile). |
| `docker/lunit-gate/invoke_lunit_tests_linux.py` | Linux equivalent of `Invoke-LUnitTests.ps1` (Python, matching this repo's existing Linux-container convention): downloads the pinned suite archive, starts LabVIEW headless, runs `LabVIEWCLI -OperationName LUnit`, validates the resulting `lunit.xml` against its own three-entry `ALLOWED_WAIVER_TEST_NAMES`. `validate_junit_report` is the pure, offline-testable policy function (`scripts/tests/test_invoke_lunit_tests_linux.py`). |
| `docker/lunit-gate/Build-LUnitLinuxImage.ps1` | Host-side orchestrator: reuses `Get-VerifiedLUnitPackageStaging.ps1` unchanged (already cross-platform), stages the package files and `invoke_lunit_tests_linux.py` into a clean build context, then builds the image. |
| `.github/workflows/lunit-gate-linux.yml` | `workflow_dispatch`, `runs-on: ubuntu-latest` -- builds the image, runs the container, validates the dependency manifest has no VIPM, runs the gate, uploads results. |

### Running it

```
gh workflow run lunit-gate-linux.yml --repo VITechnologies/projectbuddy --ref main
```

Same `suite_commit` input and `lunit-gate-linux-results-<run-id>` artifact
convention as the Windows gate.

### Fast local iteration (before touching CI)

Linux containers run natively on this build host, so there is no
separate VM harness -- iterate directly against the built image:

```
pwsh -NoProfile -File docker/lunit-gate/Build-LUnitLinuxImage.ps1 -Image lunit-gate-linux:local
docker run --rm lunit-gate-linux:local python3 /lunit-gate/invoke_lunit_tests_linux.py --suite-commit d9eac771ccac0ed92bde81a031fe2f4ebe62c845 --results-directory /lunit-gate-results
```

## Related

- `docs/architecture/adr/ADR-0003-ephemeral-container-lunit-release-gate-revised.md`
- `docs/architecture/adr/ADR-0002-adopt-ephemeral-container-lunit-release-gate.md` (superseded)
- Wiki `Software-Requirements-Specification` `REQ-033`-`REQ-036`
- `#87` (this implementation's tracking issue)
- `#62` (reopened; unrelated to this gate)
