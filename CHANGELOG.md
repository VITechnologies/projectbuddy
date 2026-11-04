# Changelog

All notable changes to this project are documented here. Versions are
published as immutable GitHub releases (tag + commit SHA); see each
release's notes for the exact pinned commit.

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

