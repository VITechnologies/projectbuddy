# VM Provisioning Procedure

- Status: Active
- Last substantive update: 2026-11-07
- Owner: svelderrainruiz

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

Both families share the same `vm_name`/`storage_root` identity discipline
(always `required: true`, generic placeholder defaults only, never
defaulting to any specific fork's real identity) -- but **the two
families currently disagree on `guest_full_name`/`guest_hostname`**:
`provision-ubuntu-vm.yml` marks both `required: true`;
`provision-windows-labview-vm.yml` marks both `required: false` with a
real, silently-applicable default (`VM Builder`/`vm-builder.local`) --
that Windows-family workflow's own input descriptions disclose this
directly (no longer self-contradictory as of `v0.3.29`: the prose now
says the generic default is real and the field "is not yet required",
matching its own schema). ISO path is a
separate, non-identity input with a legitimate generic default in both
families (see README.md's `Iso`/`GuestUser`/`WinRmHostPort` carve-out),
not part of this identity-value set.
`vm/windows-labview/batch-provision.sh` now passes `guest_full_name`/
`guest_hostname` through to each VM's `create-vm` dispatch, sourced from
optional `guestHostname`/`guestFullName` fields in `runner-inventory.json`
(schema `@4+`); if either is omitted for a given VM, a unique fallback is
derived automatically (a short, deterministic hash-based hostname that
fits Windows's 15-character NetBIOS limit, and a `VM Builder (<vm_name>)`
full name) instead of every VM in a batch silently sharing the same
hostname (`VITechnologies/projectbuddy#69`, resolved; live-verified via
a 2-VM dry-run batch -- one explicit, one derived, both distinct).

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

**Current reachability in this engine repo** (`VITechnologies/projectbuddy#62`):
`provision-public` and `complete-activation` are now both real
`workflow_dispatch`-proven end to end (`#62`): `provision-public` reaches
`labview2026-q3-pro-installed-preactivation` (run `42349506282`), and
`complete-activation` reaches `labview2026-q3-pro-activated-ready` with a
genuine, verified receipt (run `42394542555`) -- the first time either
has succeeded via real CI dispatch in this project's history.
`package-build` specifically remains blocked here by design, not by a
missing file: it calls `vm/ubuntu-labview/build-vip-package.sh`, which
hardcodes a real VI-Technologies project and is deliberately
consumer-vendored, never part of the generic engine (the same pattern
`add-project.yml` already documents for validators) -- a consuming fork
supplies its own copy. The remaining phase descriptions below describe
the *designed* behavior a properly configured consumer gets; not all of
them are yet individually re-confirmed against this engine's own
restored dispatch path.

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
- **`complete-activation`** -- **Linux-only**. Confirmed via real
  `workflow_dispatch` proof (`#62`) that this phase needs **no prior
  human-interactive LabVIEW activation at all** on this appliance --
  unlike the Windows family, this NI Linux LabVIEW distribution requires
  no separate LabVIEW license-activation dialog (the authenticated NI
  package-repository access used to install it is itself the licensing
  gate); VIPM activation (automated via `activate-vipm.sh`'s
  `vipm activate --direct-vipm-io`, see `REQ-032`) is the only activation
  this phase performs or needs. The phase's own usage text's earlier
  "run only after the operator has activated LabVIEW" wording was an
  unverified assumption carried over from the Windows family, never
  actually true for Linux -- corrected here with real evidence, not
  reworded speculation. What this phase *does* genuinely need, confirmed
  live: (1) the guest's Guest Additions version matching the host's
  (`vm/ubuntu-labview/vnc-console.sh` plus a one-time
  `virtualbox-guest-utils-hwe`/`virtualbox-guest-x11-hwe` install get a
  stale appliance there -- see "Interactive console access" below); (2)
  VIPM Desktop's own installed-package database initialized at least once
  via `xvfb-run vipm refresh` (a fresh appliance's `vipm --labview-version
  ... list --installed` query otherwise reports `LabViewVersionNotFound`
  even though LabVIEW is genuinely installed and runnable); (3) no stale
  `/tmp/.X11-unix/X*` sockets or prior partial evidence files left behind
  by an earlier failed dispatch attempt against the same disposable VM
  (both block a clean retry with their own fail-closed checks). The phase
  then completes and verifies the activation *receipt*
  (`build-activation-receipt.py`, `finalize-activation-receipt.py`,
  `verify-activation-receipt.py`), reaching the
  `labview2026-q3-pro-activated-ready` snapshot (`ACTIVATED_SNAPSHOT_NAME`).
- **`register-runner`** -- registers the VM as a GitHub Actions
  self-hosted runner for a target repo over SSH (no interactive console
  session needed), achieving functional parity with the Windows family's
  own `register-runner` phase above (`REQ-038`): a dedicated,
  non-privileged local service account runs the runner's systemd service
  (`install-actions-runner.sh` on the guest, orchestrated by
  `complete-runner-registration.sh` on the host); the pinned,
  independently-verified Actions-runner download URL/hash comes from
  `provisioning-manifest.json`'s `actionsRunner` entry. Unlike
  `complete-activation`, this phase takes no new VirtualBox snapshot and
  needs no NVMe evidence directory -- it is a safe, idempotent,
  re-runnable operation (a registered, online runner with an unlicensed
  LabVIEW/VIPM install is a safe, inert state, same principle as the
  Windows phase). Per `REQ-020`'s own established discipline (a real past
  incident where a Windows `register-runner` self-reported success while
  the runner had actually registered under the wrong name): this phase
  does not trust `config.sh`'s own exit code alone -- it polls the real
  `gh api repos/<target>/actions/runners` afterward for the expected
  runner name, `online` status, and labels before reporting PASS.
- **`package-build`** -- the strongest end-to-end proof phase: builds a
  real `.vip` package (`vi_technologies_lib_super_network_streams`)
  against the activated appliance, verifies its installation, and
  publishes the package plus a SHA-256-stamped build report as a retained
  CI artifact. Reaching this phase's `labview2026-q3-pro-package-ready`
  snapshot (`PACKAGE_SNAPSHOT_NAME`) is the pipeline's strongest signal
  that an appliance is genuinely production-ready, not merely "booted".

### Interactive console access (`vnc-console.sh`)

Ported from the Windows family's own proven `vm/windows-labview/vnc-console.sh`
(that family's "Activate LabVIEW + VIPM" runbook step, issue `#176`) --
Ubuntu had no equivalent console-access tooling at all until `#62`'s real
live-dispatch proof found it was needed to validate the Guest Additions
and GDM-greeter fixes below. `vm/ubuntu-labview/vnc-console.sh open
<vm-name>` enables VRDE on a loopback-only port (VBoxVNC, not Oracle's
RDP extension -- an RDP client cannot connect), sets a fresh random
password, and launches Remmina locally if a display is available.
Connecting remotely to the `vm-provisioner` host (no local display):
either reconnect with `ssh -X` so Remmina renders back to your own
screen, or tunnel the port out over SSH and point any VNC client at it.

Two real, Linux-container/VM-specific bugs were found and fixed getting
this working for the first time (`#62`):

- **Stale Guest Additions** (a VM built from an older appliance image can
  ship Guest Additions as old as `6.0.0` against a much newer host
  VirtualBox, e.g. `7.2.6`) cause a null framebuffer over VRDE (a
  genuinely black screen, confirmed via `VBoxManage ... screenshotpng`
  bypassing VRDE entirely) -- Oracle's own `.run` Guest Additions
  installer can itself fail to build its kernel module against a
  distro-patched kernel (a real `modpost` symbol-namespace error seen
  live); installing Ubuntu's own packaged, HWE-kernel-matched
  `virtualbox-guest-utils-hwe`/`virtualbox-guest-x11-hwe` instead resolved
  both the framebuffer and the build failure.
- **GDM's default Wayland greeter does not route VRDE-injected
  keyboard/mouse input**, even once the framebuffer itself renders
  correctly -- uncommenting `WaylandEnable=false` in
  `/etc/gdm3/custom.conf` and restarting `gdm` fixed this. This is likely
  the same root cause behind this project's earlier-documented
  Windows-family "VirtualBox GUI mode does not reliably grab
  keyboard/mouse input under Wayland desktops" finding, though that was
  never root-caused that specifically before.

### Receipt verification chain

Every meaningful state transition (base appliance, activation) produces
a receipt and a dedicated `verify-*-receipt.py` script
that independently re-checks it (`verify-base-receipt.py`,
`verify-activation-receipt.py`, `verify-public-appliance-receipt.py`,
`verify-public-provisioning-receipt.py`, `verify-vagrant-box-receipt.py`,
`verify-vagrant-clone-receipt.py`) -- mirroring `vm-fleet-status.yml`'s
"never trust a self-reported status alone" discipline for the Windows
family. The intended `package-build` transition has no dedicated
receipt verifier of its own (`package-vagrant-box.py` validates its
*input* vagrant-box receipt via `verify-vagrant-box-receipt.py`, but
produces no independently-reverifiable receipt of its own output) -- a
real gap, not yet resolved.

### LabVIEW-free lane box (`vm/ubuntu-lane/`)

A lane box is a Vagrant box of a LabVIEW-free Ubuntu 24.04 VM with a pinned
toolchain and no runner, built from a lane profile
(`projectbuddy/ubuntu-lane-profile@1`) and reusing this family's base
bootstrap receipt. Its packager writes its own
`projectbuddy/ubuntu-lane-box@1` receipt, which `Invoke-LaneBox.py
verify-receipt` recomputes from its inputs. The appliance path above is
unchanged. See the [Ubuntu lane box procedure](ubuntu-lane-box.md).

### Ubuntu lane teardown (`scripts/Invoke-Lane.py`)

Lane teardown removes one disposable lane instance by VM UUID. It powers off
only from `running` or `paused`, deletes only from `poweroff` or `aborted`,
and refuses every other state, including `livesnapshotting`. It holds a host
lock per UUID and writes a `projectbuddy/lane-teardown@1` receipt that
reports removal only after the UUID is observed absent. See the
[Ubuntu lane procedure](ubuntu-lane.md).

## Shared schemas

- `provisioning-manifest.json` (one per VM family, each with its own
  schema and content -- not a single shared shape): Windows's
  (`sandbox-sbom-reporting/windows-labview-provisioning@1`) pins
  ISO/VIPM/LabVIEW/Actions-runner/PowerShell7/NSSM binary hashes;
  Ubuntu's (`sandbox-sbom-reporting/ubuntu-labview-provisioning@1`) pins
  only ISO/VIPM/LabVIEW (no Actions-runner/PowerShell/NSSM equivalents,
  which are Windows-specific concepts). Committed, public,
  contains no credentials.
- `runner-inventory.example.json` (Windows family): a versioned schema
  (`vm-provisioning/windows-labview-runner-inventory@3`) a fork copies to
  `runner-inventory.json` (fork-local, never committed) to track its own
  real VM/runner fleet -- `vm_name`/`winrm_host_port`/runner names are
  tracked here so they never collide as more repos are onboarded onto the
  same host.
