# Changelog

All notable changes to this project are documented here. Versions are
published as immutable GitHub releases (tag + commit SHA); see each
release's notes for the exact pinned commit.

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

