# VM Provisioning Procedure

Satisfies `repo-standards-review`'s DOC/ARCH gaps for VM provisioning
(`VITechnologies/projectbuddy#29`). Describes the real, already-proven
behavior of `vm/windows-labview/` and `vm/ubuntu-labview/` -- this is a
procedural/operational document (how to run and reason about each phase),
not a requirements document; see the wiki's
[Software Requirements Specification](https://github.com/VITechnologies/projectbuddy/wiki/Software-Requirements-Specification)
(`REQ-013` through `REQ-021`) and
[Architecture Packet](https://github.com/VITechnologies/projectbuddy/wiki/Architecture-Packet)
for the governed requirements and architecture views this procedure
implements.

Both families share one discipline: every VM identity value (name, storage
root, ISO path, hostname, account name, ...) is a required
`workflow_dispatch` input with no built-in default, so a run never
silently reuses another fork's naming or infrastructure.

## Windows LabVIEW VM (`vm/windows-labview/`, `provision-windows-labview-vm.yml`)

A single-VM, 5-phase lifecycle. Each phase is independently
`workflow_dispatch`-able and individually dispatch-proven in production
(see the workflow's own header comment). Runs on a self-hosted runner
labeled `vm-provisioner` (the host that will actually run VirtualBox, not
the VM being built).

1. **`validate`** -- dry-runs `build-virtualbox.sh --dry-run --headless`
   against the supplied inputs with no real VM created. Use this to check
   an input set before committing to a real `create-vm`.
2. **`create-vm`** -- creates the real VirtualBox VM (`build-virtualbox.sh
   --run --headless`), then bootstraps WinRM over VNC
   (`vnc-bootstrap.py`), so by the time this phase succeeds the VM is both
   built and WinRM-reachable -- not a separate manual follow-up step. This
   replaced an earlier `FirstLogonCommands`-based mechanism, retired
   because an in-guest `winlogon.exe`-initiated restart unconditionally
   interrupted it. The target OS/hardware profile
   (`os`/`virtualBox` sections of `provisioning-manifest.json`) is
   **double-pinned**: `build-virtualbox.sh` reads these values from the
   manifest *and* cross-checks each one against its own hardcoded literal
   expectation (`Windows11_64`, the exact ISO filename/SHA-256, 120GB/
   8192MB/4-CPU/128MB-VRAM, `efi64`/`ich9`/TPM 2.0), refusing to proceed if
   either source disagrees -- a manifest edit alone can never silently
   redirect a run to an unvalidated profile. `unattended-template.xml`
   (the actual Windows Setup answer file) is a **vendored, near-verbatim
   copy** of VirtualBox's own shipped template, deliberately kept
   byte-close to upstream (including upstream's own stale/generic
   comments) specifically to keep future re-vendoring against a newer
   VirtualBox version a tractable diff -- see the file's own header
   comment for the one documented additive change.
3. **`install-vipm`** -- verifies the supplied VIPM installer's SHA-256
   against `provisioning-manifest.json`'s pinned hash (fails closed on
   mismatch, before building any install media), wraps it into an ISO,
   attaches it, and installs VIPM Professional over WinRM. Shutting the VM
   down first (if already running) uses a deliberate fallback order:
   WinRM-driven shutdown first, then ACPI power-button, then a hard
   poweroff -- never skipping straight to a hard poweroff, since
   `acpipowerbutton` alone has been observed to leave WinRM responsive
   while the guest never actually reaches `poweroff`. After the restart,
   readiness is confirmed via a real WinRM round-trip request (not a bare
   TCP-port check, which was observed to report ready before the
   SOAP/PowerShell-remoting service could actually handle requests).
4. **`install-labview`** -- same attach/restart/readiness-poll mechanics
   as `install-vipm`, installing the operator-supplied LabVIEW
   Professional ISO instead. This phase now also verifies the supplied
   ISO's SHA-256 against `provisioning-manifest.json`'s
   `labview.installer.sha256` before attaching it, fail-closed on
   mismatch, exactly matching `install-vipm`'s own guarantee (closed via
   a real `workflow_dispatch` validation against an actual VM, confirming
   both the valid-hash and mismatched-hash paths; `VITechnologies/projectbuddy#38`).
5. **`register-runner`** -- registers the VM as a GitHub Actions
   self-hosted runner for a target repo over WinRM (no interactive
   session needed), using a dedicated local Windows service account (not
   the operator's own). Reads the exact pinned Actions-runner and
   PowerShell-7 download URLs/hashes from `provisioning-manifest.json`.
   **Caution**: the step deliberately names its target-runner-name
   variable `TARGET_RUNNER_NAME`, not `RUNNER_NAME` -- `RUNNER_NAME` is a
   GitHub Actions *reserved* environment variable (already set to the
   *host* runner's own name) and a step-level `env:` of that name is
   silently ignored, confirmed live after every VM registered so far
   initially inherited the host's own runner name instead of its intended
   one.

**LabVIEW/VIPM activation is deliberately never an automated phase.** It
is the one step that must stay human-driven and interactive -- not an
oversight or a not-yet-implemented phase. A VM that completes phase 5 is
already a fully registered, online GitHub Actions runner; it is simply
not yet licensed. `batch-provision.sh` serially drives phases 1-5 across
every `"status": "queued"` entry in `runner-inventory.json`, explicitly
stopping at "ready for the operator to activate" for exactly this reason
(and because concurrent first-boot VMs were observed to contend heavily
for host CPU/disk during Windows Update/Defender housekeeping).

### Fleet status (`vm-fleet-status.yml`)

Runs on a 30-minute schedule (plus `workflow_dispatch` and a push trigger
on `runner-inventory.json` changes). This is an **active verification
gate, not a passive status mirror**: each matrix job independently
re-derives ground truth (the real GitHub Actions runner registration via
`gh api .../actions/runners`, and the real VirtualBox power state) rather
than trusting `runner-inventory.json`'s own self-logged status field.
This design exists because a logged "success" was observed live to not
guarantee the real-world outcome matched: a `register-runner` run
reported success while the runner had actually registered under the
wrong name (see the `RUNNER_NAME` caution above) -- a discrepancy only
caught by directly querying the GitHub API, not by trusting the
workflow's own conclusion.

## Ubuntu LabVIEW VM (`vm/ubuntu-labview/`, `provision-ubuntu-vm.yml`)

A snapshot-based appliance lifecycle with 10 phases, orchestrated from
*either* a Linux or a Windows self-hosted `vm-provisioner` runner
(`runner_os` dispatch input selects which); the Linux (`build-virtualbox.sh`)
and Windows (`build-virtualbox.ps1`) adapter scripts take the same
identity parameters so the choice of orchestrating host OS never
constrains which guest family can be built.

- **`validate`** -- dry-runs the Linux and/or Windows adapter plan plus a
  public-plan contract check (`.github/scripts/validate-vm-provisioning-dispatch.py`),
  no real VM touched.
- **`inspect-public`** -- reads an existing public appliance's real state
  without changing it.
- **`console-fingerprint`** -- captures the guest's SSH host key
  fingerprint directly from the VirtualBox console (not over the network),
  for out-of-band trust establishment before the first real SSH
  connection.
- **`repair-ssh-console`**, **`repair-public`** -- narrow, targeted repair
  operations for a specific known-broken state, not a general-purpose
  reset.
- **`restart-public`**, **`resume-public`** -- lifecycle control over an
  existing appliance (restart vs. resume from a saved/suspended state).
- **`provision-public`** -- the real build: creates the appliance via the
  OS-appropriate adapter script, reaching the
  `labview2026-q3-pro-installed-preactivation` snapshot (`PREACTIVATION_SNAPSHOT_NAME`
  in `complete-public-provisioning.py`/`verify-public-appliance-receipt.py`)
  -- installed, but not yet activated.
- **`complete-activation`** -- **Linux-only, and only reachable after an
  operator console gate**: the operator interactively activates
  LabVIEW/VIPM themselves (the same human-only boundary as the Windows
  family), and this phase then completes and verifies the activation
  *receipt* (`build-activation-receipt.py`, `finalize-activation-receipt.py`,
  `verify-activation-receipt.py`), reaching the
  `labview2026-q3-pro-activated-ready` snapshot (`ACTIVATED_SNAPSHOT_NAME`)
  -- it does not perform activation itself.
- **`package-build`** -- the strongest end-to-end proof phase: builds a
  real `.vip` package (`vi_technologies_lib_super_network_streams`)
  against the activated appliance, verifies its installation, and
  publishes the package plus a SHA-256-stamped build report as a retained
  CI artifact. Reaching this phase's `labview2026-q3-pro-package-ready`
  snapshot (`PACKAGE_SNAPSHOT_NAME`) is the pipeline's strongest signal
  that an appliance is genuinely production-ready, not merely "booted".

### Receipt verification chain

Every meaningful state transition (base appliance, activation, package
build) produces a receipt and a dedicated `verify-*-receipt.py` script
that independently re-checks it (`verify-base-receipt.py`,
`verify-activation-receipt.py`, `verify-public-appliance-receipt.py`,
`verify-public-provisioning-receipt.py`, `verify-vagrant-box-receipt.py`,
`verify-vagrant-clone-receipt.py`) -- mirroring `vm-fleet-status.yml`'s
"never trust a self-reported status alone" discipline for the Windows
family.

## Shared schemas

- `provisioning-manifest.json` (one per VM family): a versioned schema
  (`sandbox-sbom-reporting/windows-labview-provisioning@1` for Windows)
  pinning the exact ISO/VIPM/LabVIEW/Actions-runner/PowerShell/NSSM
  binary hashes this pipeline is proven against. Committed, public,
  contains no credentials.
- `runner-inventory.example.json` (Windows family): a versioned schema
  (`vm-provisioning/windows-labview-runner-inventory@3`) a fork copies to
  `runner-inventory.json` (fork-local, never committed) to track its own
  real VM/runner fleet -- `vm_name`/`winrm_host_port`/runner names are
  tracked here so they never collide as more repos are onboarded onto the
  same host.
