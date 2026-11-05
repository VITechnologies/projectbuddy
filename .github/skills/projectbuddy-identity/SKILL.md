---
name: projectbuddy-identity
description: 'Orient on what ProjectBuddy is: a public, project-agnostic engine (maintained by VI Technologies) for provisioning self-hosted Windows/Ubuntu LabVIEW build VMs, registering them as GitHub Actions self-hosted runners, and running the native SBOM reporting pipeline. Use when orienting a new agent/session on this repo, when a request involves VM lifecycle (create-vm/teardown/enable-vrde), runner registration, or SBOM pipeline execution, or when deciding whether a change belongs here (public, project-agnostic) versus a downstream consumer repo (private, project-specific).'
---

# ProjectBuddy identity

## What this is
ProjectBuddy is a **public, project-agnostic engine** for:
- provisioning self-hosted build/test VMs via VirtualBox (Windows 11 +
  LabVIEW/VIPM under `vm/windows-labview/`, Ubuntu 24.04 + LabVIEW/VIPM
  under `vm/ubuntu-labview/`),
- registering those VMs as GitHub Actions self-hosted runners, and
- running the native (non-container) SBOM build/scan/report pipeline
  (`Run-Pipeline.ps1` / `Run-Pipeline.sh`, `Build-Sbom.ps1`) that those
  runners execute.

It ships with **zero pre-committed project configs, VI
Technologies-specific identifiers, or private infrastructure state** --
every VM identity value and project config is a required input, supplied
by whichever downstream repo consumes this engine. `runner-inventory.json`
and real project `.toml`/`.json` configs are fork-local, never committed
upstream.

## Relationship to SandboxSBOMReporting
`VITechnologies/SandboxSBOMReporting` is the primary private consumer: it
supplies its own real project configuration and infrastructure state, and
consumes this engine's reusable workflows/scripts as a versioned, pinned
dependency (`uses: VITechnologies/projectbuddy/.github/workflows/<name>.yml@<tag>`
for workflows; a second pinned `actions/checkout` for directly-invoked
scripts). Upgrades are deliberate, reviewed version-bump commits -- never a
floating branch or `latest`. **Never the other way around**: no
SandboxSBOMReporting-specific identifiers, credentials, or private
configuration should ever flow into this engine repo.

Active engineering (VM provisioning bug fixes, NSIS host-bootstrap work,
runner registration) happens on `svelderrainruiz/projectbuddy`, a personal
fork of the `VITechnologies/projectbuddy` public upstream, used to activate
and iterate on real VMs without requiring org-level review for every
in-flight fix. Changes validated there are expected to flow back upstream
to `VITechnologies/projectbuddy` once stable.

## Key entry points
- `Register-WindowsRunner.ps1` -- Windows self-hosted runner
  (re-)registration; defaults to a plan, requires `-AuthorizeActionsStatus`
  and `-Run`/`ShouldProcess` to mutate.
- `Add-Project.ps1` -- registers a new project via the `add-project.yml`
  workflow (validates `pipelineConfig` + VIPM TOML, upserts
  `PROJECTS_OVERRIDE`/`VIPM_TOML_CONFIGS`).
- `Run-Pipeline.ps1` / `Run-Pipeline.sh` -- native SBOM pipeline entry
  points consumed by activated runners.
- `.github/workflows/provision-windows-labview-vm.yml` -- the main VM
  lifecycle workflow (`validate`/`create-vm`/`install-vipm`/
  `install-labview`/`register-runner`/`teardown`/`enable-vrde` phases).
- [NSIS host/guest installer patterns](../nsis-host-installers/SKILL.md) --
  companion skill for the `.nsi`-based bootstrap installers this engine
  builds and runs.

## Known open threads (consult before assuming a problem is new)
- Issue #188 (closed, SandboxSBOMReporting): a `winlogon.exe`-initiated
  restart during Windows Setup interrupts first-boot WinRM automation.
  Issue #191's closing comment documents a **negative result**: neither
  RunOnce-from-specialize-pass nor RunOnce-prepended-to-oobeSystem survived
  this restart -- the race appears to occur at the OS/logon level, before
  the command chain is even reached, regardless of scheduling mechanism.
  Treat any new "register a command/service to survive this restart" idea
  as needing to explain why it differs structurally from both tested
  variants, not just re-propose a third registration mechanism.
- Issue #280 (SandboxSBOMReporting): VITLT-SERGIO host has no working VNC
  Extension Pack (Oracle's official Windows build no longer ships a VRDE
  module at all), blocking the current VNC-dependent guest-bootstrap flow
  (`vnc-bootstrap.py`) on that host specifically.

## References
- [README.md](../../../README.md) -- full engine description, consumption
  model, and `vm/windows-labview/` + `vm/ubuntu-labview/` details.
- [CHANGELOG.md](../../../CHANGELOG.md) -- released versions.
