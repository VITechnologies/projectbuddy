# Changelog

All notable changes to this project are documented here. Versions are
published as immutable GitHub releases (tag + commit SHA); see each
release's notes for the exact pinned commit.

## v0.3.9

Adopt GitFlow branch governance (`develop` branch, `feature/*`,
`release/*`, `hotfix/*`), per operator decision on `VITechnologies/projectbuddy#6`,
to satisfy `svelderrainruiz/repo-standards-review`'s CM gate — see
`docs/architecture/adr/ADR-0001-adopt-gitflow-for-release-governance.md`
and `docs/release/release-procedure.md`. The decision's condition: every
compliance document stamps the exact `repo-standards-review` version (tag
+ commit SHA) it was assessed against — see
`docs/architecture/standards-compliance.md`, which also documents a weekly,
flag-only (never auto-editing) re-certification check
(`.github/workflows/standards-compliance-recheck.yml`). The
`svelderrainruiz/projectbuddy` fork's `main` now plays GitFlow's `develop`
role; sync pull requests target this repo's `develop` branch going forward,
not `main`.

## v0.3.8

Add `Enable-RepoIssueTracking.ps1`: enables Issues (idempotent, never
disables) on a given `owner/repo` and seeds the `needs-human-decision`
label. `add-project.yml` now calls it automatically, as a best-effort step,
for every GitHub-hosted repo a newly-registered project's config
references (`repoUrl`, `sourceCheckouts[].repository`,
`build.lvlibpDependencies[].repository`); this needs a new
`ACTIONS_REPO_ADMIN_TOKEN` secret and only warns (never fails project
registration) if it is missing or insufficiently scoped. Fixes
`VITechnologies/projectbuddy#4`.

## v0.3.7

Add `New-HumanEscalation.ps1` and `Get-HumanEscalations.ps1`: a first-class,
project-agnostic mechanism for flagging a blocker that automated iteration
cannot resolve on its own, instead of leaving that determination buried in
an ad-hoc issue comment. `New-HumanEscalation.ps1` creates or (re-run with
the same `-EscalationId`) idempotently updates a structured GitHub issue
(context, why the agent could not resolve it, options considered, a
recommendation, related issues) under a consistent `needs-human-decision`
label. `Get-HumanEscalations.ps1` queries one or more repos for open
escalations and prints a single consolidated dashboard.

## v0.3.6

`Run-WindowsSbom.ps1`/`Run-LinuxSbom.ps1` now mount their own location
(`$PSScriptRoot`) as a second, separate `C:\engine`/`/engine` container
mount, alongside the existing consumer-repository workspace mount.
`windows_sbom_pipeline.ps1`, `Build-LvlibpCandidate.ps1`, and
`linux_sbom_pipeline.py` previously resolved `Build-Sbom.ps1`,
`pipeline-config.schema.json`, and `vipm-exit-codes.json` relative to the
single workspace mount, which used to also hold the consumer's own copy of
these files. Once a consumer stops keeping that local copy (consuming this
repository via a pinned checkout instead, per
`docs/projectbuddy-consumption.md`), those reads silently failed inside the
container. The container-side scripts now resolve engine-root files from
the new engine mount, falling back to the workspace mount when no separate
engine mount is present. Mirrors the `$PSScriptRoot`/`-WorkspaceRoot` split
already used by `Run-Pipeline.ps1`, `Add-Project.ps1`, etc. for the native
(non-container) pipeline.

## v0.3.5

`Add-Project.ps1` now accepts `-WorkspaceRoot` (defaulting to `$PSScriptRoot`,
so direct in-repo/local usage is unaffected), used to resolve
`.github/scripts/project_runtime.py`, `.github/scripts/validate-project-registration.py`,
its `git branch --show-current` lookup, and its `gh workflow run` dispatch
working directory. Those `.github/scripts/*` validators stay local-only to
each consumer (never published here), so this silently broke the first time
`Add-Project.ps1` was actually invoked from a pinned checkout instead of a
consumer's own repository root. Mirrors the same `-WorkspaceRoot` pattern
already applied to the other entry scripts in v0.3.0, for the identical
class of problem.

## v0.3.4

Captures bounded, redacted failure diagnostics (process/window snapshot --
the same mechanism already proven for the per-package `library_add_package`
stall) when the manifest dependency install (`vipm install -y`) fails with
the `package_set_install` stall signature. Written to
`packages/manifest-dependency-failure-diagnostics.json`, then the original
error is re-thrown unchanged -- this install is still never retried (v0.3.3
established that retrying here cannot recover a hung LabVIEW and risks
masking a different, unrecoverable hang as a transient stall). Diagnostic
capture is best-effort: a capture failure logs a warning but never masks
the original stall error. `VITechnologies/SandboxSBOMReporting`'s issue
#122 (bounded root-cause evidence capture) already covered the per-package
case; this closes the remaining gap the #240/#244 investigation found --
diagnosing that stall took a multi-hour interactive investigation because
this exact path had zero automated evidence.

## v0.3.3

Reverts v0.3.1/v0.3.2's `Invoke-ManifestDependencyInstall` /
`Stop-StalledVipmDesktop` retry-and-kill-restart logic for the
`package_set_install` manifest dependency stall. A live native end-to-end
re-test of this exact mechanism on `VITechnologies/SandboxSBOMReporting`'s
issue #240 showed the kill-and-retry correctly stopped and restarted the
stalled VIPM Desktop, reconnected cleanly, and still hit the identical
stall on the same package -- proving the stall was never a frozen process.
Interactive host investigation (issue #244) then found the real cause:
LabVIEW's UI thread hanging at startup under a dedicated runner service
account because of the Viewpoint TSVN Toolkit project provider, so VI
Server reset every VIPM connection. That is fixed on the host, not in
pipeline code, and a hung LabVIEW cannot be recovered by retrying or
restarting VIPM Desktop around it -- a retry just re-triggers the same
hang and risks masking a future, different, genuinely-unrecoverable LabVIEW
hang as a transient stall instead of failing fast. Manifest dependency
install is therefore a bare `vipm install -y` call again, matching the
consumer's own reviewed and accepted behavior. v0.3.1/v0.3.2 remain
published (GitHub immutable releases) but are superseded; do not pin to
them.

## v0.3.2

`Invoke-ManifestDependencyInstall`'s `package_set_install` stall retry
(v0.3.1) now stops the stalled VIPM Desktop process (forcibly, waiting for
exit) before retrying, mirroring the Docker container pipeline's existing
`Stop-Process -Force` + `WaitForExit` restart pattern. Live validation of
v0.3.1 alone showed the retry fired correctly but then hung for 10 minutes
on VIPM's own "wait for VIPM startup" readiness check: the vipm CLI could
not initialize a fresh Desktop while the first attempt's already-stalled
instance was still resident and holding its resources.

## v0.3.1

`Build-Sbom.ps1`'s manifest dependency install (`vipm install -y`, resolving
and installing a project's `vipm.toml` `[dependencies]` before any package
build) now retries once with `VIPM_DEBUG=1` when VIPM reports its
`package_set_install` command made no progress -- the same recovery already
proven for the per-package `library_add_package` stall. Found live-debugging
`VITechnologies/SandboxSBOMReporting`'s issue #240: a real dependency-set
install stalled with true zero progress for minutes on end, with no retry
path at all, unlike the sibling per-package install which already recovers
from the equivalent stall automatically.

## v0.2.1

`vm/windows-labview/vnc-bootstrap.py` now accepts an explicit `--inventory`
flag (passed through to `vnc-console.sh`), needed when this script runs
from a location other than where a consumer's real `runner-inventory.json`
lives (e.g. checked out as a pinned release into a subdirectory) -- found
while wiring `VITechnologies/SandboxSBOMReporting`'s real production
cutover (epic #207's #212): without this, the consumer's pinned
`vncPort`/`winrmHostPort` lookups would silently miss (falling back to
this engine's own derived-port logic instead of the consumer's real,
intentional inventory values).

## v0.2.0

First real engine release: the VM-provisioning pipeline
(`vm/windows-labview/`, `vm/ubuntu-labview/`) and the native SBOM reporting
pipeline (root-level `Run-*`, `Build-Sbom.ps1`, `internal/`, `docker/`),
generalized out of `VITechnologies/SandboxSBOMReporting` (that repository's
epic #207, issues #210/#211). Every VM/project identity value is a
required input with no built-in default; Dependency-Track reporting
requires an explicit `DEPENDENCY_TRACK_URL` with no default pointing at any
particular organization's server. See [README.md](README.md)'s "What's in
this engine" section for the full content breakdown.

## v0.1.1

Fixes `toolchain-smoke-test.yml`: a reusable (`workflow_call`) workflow's
default `actions/checkout` checks out the **caller's** repository, not this
one -- `github.repository` resolves to the caller inside a called
workflow's job. Added a required `projectbuddy-ref` input so callers pass
back the exact same commit SHA they pinned in their own `uses:@<ref>` line,
and the checkout step now explicitly targets
`repository: VITechnologies/projectbuddy` at that ref. Confirmed live
2026-11-04: v0.1.0's reusable-workflow path failed with "term
'./scripts/Test-ToolchainPresence.ps1' is not recognized" because it had
checked out the caller's repository instead.

## v0.1.0

Initial public release: establishes the reusable-workflow/script consumption
mechanism (see [README.md](README.md)) with a minimal, side-effect-free
toolchain smoke-test workflow and script as a proof of the mechanism.

