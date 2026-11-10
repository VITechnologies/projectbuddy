# Ubuntu Lane Box Procedure

- Status: built and fixture-tested offline (`VITechnologies/projectbuddy#183`,
  `VITechnologies/projectbuddy#186`); not yet live-proven.
- Last substantive update: 2026-11-09
- Owner: svelderrainruiz

A lane box is a Vagrant box of a LabVIEW-free Ubuntu 24.04 VM. It carries a
pinned toolchain and no LabVIEW, VIPM, license state, or GitHub Actions
runner. It is the base image for the Ubuntu lane in the wiki's
[REQ-101](https://github.com/VITechnologies/projectbuddy/wiki/REQ-101) to
[REQ-103](https://github.com/VITechnologies/projectbuddy/wiki/REQ-103). The
lane plan, teardown, and clone verifier that consume it are
`VITechnologies/projectbuddy#185`.

The LabVIEW appliance under `vm/ubuntu-labview/` is unchanged. It keeps its
own manifest, packager, and `projectbuddy/ubuntu-vagrant-box@1` receipt (see
[the VM provisioning procedure](procedure.md#ubuntu-labview-vm-vmubuntu-labview-provision-ubuntu-vmyml)).

The pieces:

- [`scripts/Invoke-LaneBox.py`](../../scripts/Invoke-LaneBox.py): the command
  line, used in the guest and on the host. It needs Python 3.11 or later;
  Ubuntu 24.04 ships 3.12.
- [`scripts/lane_box/`](../../scripts/lane_box/): the profile, toolchain,
  readiness, packager, and receipt modules.
- [`vm/ubuntu-lane/lane-profile.example.json`](../../vm/ubuntu-lane/lane-profile.example.json):
  the example lane profile for the VS Code extension lane.
- [`vm/ubuntu-lane/Vagrantfile.box`](../../vm/ubuntu-lane/Vagrantfile.box):
  the policy embedded in every lane box. It disables the communicator and the
  synced folder, and binds the SSH forward to `127.0.0.1` only.

## What the lane box guarantees

- Every downloaded tarball and `.deb` is checked against its pinned SHA-256
  before anything is installed.
- The guest was scanned for Actions runner configuration, runner services,
  and LabVIEW, VIPM, or NI files and packages, and none were found.
- Every toolchain probe printed the version that the profile expects.
- The box holds the named snapshot of the named VM, which was powered off
  and at that current snapshot. The packager exports a temporary full clone
  of the snapshot, so changes made to the VM after the snapshot are not
  packaged. The VM and the clone both have the profile's memory, CPUs,
  VRAM, and disk size.
- The source VM is only read. `vagrant package` deletes the port forwards
  and shared folders of the VM it packages, so it runs on the clone.
- The box receipt holds no credential fields, and `verify-receipt`
  recomputes it from its inputs. Any edit to the box, profile, base receipt,
  readiness receipt, or embedded Vagrantfile makes it fail.

## What it does not guarantee

- **That the snapshot is the scanned state.** The packager cannot prove that
  nothing changed between the readiness scan and the snapshot. The receipt
  records the scan's boot ID as `runnerAbsence.checkedBootId`. Power off and
  take the snapshot right after the scan.
- **Pinned `.deb` dependencies.** Only the listed `.deb` files are pinned.
  Their dependencies come from the guest's signed apt sources.
- **Cleanup after a hard kill.** If the packager is killed with `SIGKILL`,
  or the host loses power, the temporary clone and partial outputs can
  remain. If an interrupted `VBoxManage clonevm` dies before VirtualBox
  confirms the cancel, VirtualBox can finish the clone after the packager
  exits; the packager names the clone when this can happen. Step 6
  explains how to remove them.
- **A live proof.** No lane box has been built from a real VM yet.

## The lane profile

A lane profile (`projectbuddy/ubuntu-lane-profile@1`) is a JSON object with
exactly these fields:

| Field | Meaning |
| --- | --- |
| `schema` | `projectbuddy/ubuntu-lane-profile@1` |
| `name` | the lane name, recorded in every receipt |
| `os` | the same shape as the appliance manifest's `os` block. It must be `ubuntu`, `24.04`, `noble`, with an `https` ISO URL and its SHA-256 |
| `virtualBox` | `diskGb`, `memoryMb`, `cpus`, and `vramMb`, each a positive integer. The same shape as the appliance manifest's `virtualBox` block |
| `toolchain` | a non-empty list of tools, each with a unique name |

Each tool has exactly `name`, `version`, `source`, and `probe`. The source is
one of:

| `source.kind` | Fields | Installed as |
| --- | --- | --- |
| `tarball` | `url` (`https`, ending `.tar.xz`, `.tar.gz`, or `.tgz`) and `sha256` | one top-level directory moved to `<prefix>/<name>-<version>`, with each file in its `bin/` linked into the bin directory |
| `deb` | `url` (`https`, ending `.deb`) and `sha256` | all `.deb` files in one `apt-get install --no-install-recommends` call |
| `bundled` | `in`: the name of a `tarball` tool | nothing more; it ships inside that tarball (npm inside Node) |

The probe has `argv`, `pattern`, and `expected`. The probe runs `argv`
without a shell; `install-toolchain` puts the bin directory first on `PATH`.
The probe passes when it exits 0, `pattern` (a regular expression with
exactly one capture group) matches its whole trimmed output, and the
captured text equals `expected`.

A lane profile is refused when it has a `labview`, `vipm`, or `actionsRunner`
field, or any field not listed above. It is also refused when a tool's name,
version, or URL names LabVIEW, VIPM, NI, or the Actions runner, or when a
download comes from `download.ni.com`.

Check a profile on any machine:

```bash
python3 scripts/Invoke-LaneBox.py validate-profile vm/ubuntu-lane/lane-profile.example.json
```

### Updating pins

- Node: take the tarball's SHA-256 from the release's `SHASUMS256.txt` on
  `nodejs.org`. Update npm's `version` and `expected` to the npm that release
  bundles.
- `.deb` files: take the pool URL and SHA-256 from the Ubuntu archive's
  `Packages` index for `noble`, `noble-updates`, or `noble-security`. If the
  guest already has a newer version of a pinned package, apt refuses the
  downgrade and `install-toolchain` fails. Pin the guest's version instead.

## Building a lane box

The steps alternate between the guest (as root) and the host. Commands run
from a clone of this repository unless they say otherwise. The profile file
must be byte-identical everywhere: the readiness receipt records its SHA-256,
and the packager compares it.

### 1. Build the base VM (host)

Build an Ubuntu 24.04 VM from the lane profile with
[`vm/ubuntu-labview/build-virtualbox.sh`](../../vm/ubuntu-labview/build-virtualbox.sh),
so that its first boot writes
`/var/lib/projectbuddy/base-bootstrap-receipt.json`
(`projectbuddy/ubuntu-base-bootstrap@2`, outcome `PASS`):

```bash
LANE_PROFILE=vm/ubuntu-lane/lane-profile.example.json \
VM_NAME=<VM name> BASEFOLDER=<NVMe directory> \
GUEST_FULLNAME=<full name> GUEST_HOSTNAME=<host name> \
  vm/ubuntu-labview/build-virtualbox.sh
```

This prints the plan and changes nothing. To build, add `--run` and set
`ISO` to the downloaded ISO and `GUEST_PASSWORD_FILE` to a non-empty file
with mode `0600` or stricter.

With `LANE_PROFILE` set, the builder:

- validates the profile with the same code as `validate-profile` and prints
  its name and SHA-256, which the readiness receipt and the packager must see
  unchanged;
- takes the memory, CPUs, VRAM, and SATA port 0 disk size from the profile's
  `virtualBox` block, so the VM matches what the packager checks;
- keeps the appliance's OS and ISO pins: the profile must name
  `Ubuntu24_LTS_64` and the appliance manifest's ISO filename, URL, and
  SHA-256, the only ISO the unattended install and `base-bootstrap.sh` are
  tested with;
- keeps the appliance's other refusals: the VM name, guest user, and SSH
  port checks, and, with `--run`, an existing VM of the same name, the ISO
  digest, the password file's mode, and a `BASEFOLDER` that is not on a
  writable internal NVMe filesystem;
- refuses `LANE_PROFILE` together with `PROVISIONING_MANIFEST`.

A refused profile, OS or ISO pin, or identity value exits 2, and a missing
`VBoxManage` with `--run` exits 2. The other `--run` checks exit 3, except
the ISO digest check, where `sha256sum --check` fails with exit 1. All of
these checks run before the plan is printed and before any VM is created.
Without
`LANE_PROFILE`, the builder reads the appliance manifest and still requires
the appliance hardware (80 GB, 12288 MB, 6 CPUs, 128 MB VRAM).

Power off and take a snapshot before step 2, so that a failed install can be
undone.

### 2. Install the toolchain (guest, root)

Copy `scripts/Invoke-LaneBox.py`, the `scripts/lane_box/` directory, and the
profile into one guest directory, keeping `lane_box/` next to
`Invoke-LaneBox.py`. Then:

```bash
sudo python3 Invoke-LaneBox.py install-toolchain --profile lane-profile.json
```

It downloads every tarball and `.deb` into a new private directory and checks
every digest. Then it installs the tarballs under `/opt/projectbuddy-lane`
(`--prefix`), links their commands into `/usr/local/bin` (`--bindir`),
installs the `.deb` files, and runs every probe. It never replaces an
existing command or install directory.

A digest mismatch or download failure installs nothing and exits 2. A
failure after something was installed exits 1 and names what was changed;
restore the snapshot from step 1.

### 3. Write the readiness receipt (guest, root)

```bash
sudo python3 Invoke-LaneBox.py readiness --profile lane-profile.json
```

It writes `lane-readiness-receipt.json` and its `.sha256` file to
`/var/lib/projectbuddy` (`--output-dir`), both mode 600. The scan:

- reads the VM name and UUID from the base bootstrap's state file, and the
  boot ID from the kernel;
- walks the whole filesystem except `/dev`, `/proc`, `/run`, and `/sys`,
  without following symlinks. Any directory that holds an entry named
  `.runner` or `.credentials`, and any entry named `actions.runner.*`, is a
  runner finding. An unreadable directory makes the scan incomplete;
- looks for `/etc/natinst`, `/opt/natinst`, and `/usr/local/natinst`, and for
  installed dpkg packages that name LabVIEW, VIPM, NI, or the Actions runner;
- runs every toolchain probe.

Any finding makes the outcome `FAIL` and the exit code 1. The receipt is
still written, and the packager refuses it. Delete the copied scripts and
profile from the guest afterwards; the receipt does not depend on them.

### 4. Collect the evidence (host)

Copy three files from the guest's `/var/lib/projectbuddy` into an evidence
directory with mode 700: `base-bootstrap-receipt.json`,
`lane-readiness-receipt.json`, and `lane-readiness-receipt.json.sha256`.
Keep the file names. Each file must have mode 600. For example, over the
VM's loopback SSH forward, as a guest user who can run `sudo` without a
terminal:

```bash
umask 077
mkdir -p "$EVIDENCE"
for name in base-bootstrap-receipt.json lane-readiness-receipt.json lane-readiness-receipt.json.sha256; do
  ssh -p "$SSH_HOST_PORT" "$GUEST_USER@127.0.0.1" sudo cat "/var/lib/projectbuddy/$name" > "$EVIDENCE/$name"
done
```

### 5. Power off and take the snapshot (host)

Shut the guest down, wait until `VBoxManage showvminfo` reports
`VMState="poweroff"`, and take a snapshot. Record its name and UUID from
`CurrentSnapshotName` and `CurrentSnapshotUUID`. The packager never powers a
VM off or takes a snapshot.

### 6. Package (host)

Without `--run`, `package` runs every check and prints the plan:

```bash
python3 scripts/Invoke-LaneBox.py package \
  --profile vm/ubuntu-lane/lane-profile.example.json \
  --vm-name "$VM_NAME" --vm-uuid "$VM_UUID" \
  --snapshot-name "$SNAPSHOT_NAME" --snapshot-uuid "$SNAPSHOT_UUID" \
  --evidence-dir "$EVIDENCE" \
  --output "$OUT/lane.box" --receipt-out "$OUT/lane-box-receipt.json" \
  --vagrant-home "$LANE_VAGRANT_HOME" --temp-dir "$LANE_TEMP"
```

The evidence, output, Vagrant home, and temporary directories must exist with
mode 700, and the box, receipt, and `.sha256` outputs must not exist yet. The
checks are:

- the VM and snapshot names and UUIDs are valid, and the output paths are
  absolute;
- the profile, the base receipt, and the readiness receipt pass, and both
  receipts name this VM. The readiness receipt names this profile and
  matches its `.sha256` file;
- the registered VM has this name and UUID, is powered off, and is at this
  current snapshot;
- its memory, CPUs, VRAM, and SATA port 0 disk capacity equal the profile's
  `virtualBox` block.

Add `--run` to package. It repeats the VM checks and exits 2 without
changing anything if one fails. Then it:

1. clones the snapshot into a new VM named `lane-pack-<new UUID>` under the
   temporary directory, with
   `VBoxManage clonevm <VM UUID> --snapshot=<snapshot UUID> --mode=machine --options=KeepAllMACs,KeepHwUUIDs --register`.
   The clone is a full copy of the snapshot's disk and settings. It keeps
   the MAC addresses, so the box's `base_mac` matches the source VM.
2. checks the clone's memory, CPUs, VRAM, and disk against the profile;
3. runs `vagrant package --base <clone UUID> --vagrantfile vm/ubuntu-lane/Vagrantfile.box`
   with `VAGRANT_HOME` and `TMPDIR` set to the given directories;
4. deletes the clone with `VBoxManage unregistervm <clone UUID> --delete`
   and removes its directory, whether packaging passed or failed.

The clone and Vagrant steps each have a one-hour timeout, and the delete
step has a ten-minute timeout. The temporary directory needs free space for
a full copy of the VM's disk and for Vagrant's export. The packager then
writes the box and the receipt with mode 600, verifies the receipt, and
writes `lane-box-receipt.json.sha256`.

If anything fails after cloning starts, or the packager gets `Ctrl-C` or
`SIGTERM`, it sends `SIGTERM` to the running `VBoxManage` or `vagrant`
process group, waits up to 30 seconds, and then sends `SIGKILL`. A second
`Ctrl-C` during that wait sends `SIGKILL` at once. On `SIGTERM`,
`VBoxManage` cancels its VirtualBox job and waits until VirtualBox has
removed what the job created. The packager then deletes the clone, removes
all three outputs, and exits 1. If the clone cannot be deleted, or
`VBoxManage clonevm` died before VirtualBox confirmed the cancel, the
message names the clone and how to remove it. After a `SIGKILL` of the
packager, look for a `lane-pack-` VM in `VBoxManage list vms`. Remove it with
`VBoxManage unregistervm <clone UUID> --delete`, then remove its directory
under the temporary directory and any partial outputs.

`--vboxmanage` and `--vagrant` name the tools when they are not on `PATH`.

### 7. Verify the box (host)

Verify the box at any time, and before every use:

```bash
python3 scripts/Invoke-LaneBox.py verify-receipt "$OUT/lane-box-receipt.json" \
  --box "$OUT/lane.box" \
  --profile vm/ubuntu-lane/lane-profile.example.json \
  --base-receipt "$EVIDENCE/base-bootstrap-receipt.json" \
  --readiness-receipt "$EVIDENCE/lane-readiness-receipt.json" \
  --vm-name "$VM_NAME" --vm-uuid "$VM_UUID" \
  --snapshot-name "$SNAPSHOT_NAME" --snapshot-uuid "$SNAPSHOT_UUID"
```

The `--vm-*` and `--snapshot-*` pairs are optional; when given, the receipt
must name that VM and snapshot. Verification checks that the receipt and box
have mode 600, rechecks both input receipts, and checks that the box holds
exactly one generated `Vagrantfile` and an `include/_Vagrantfile` equal to
the embedded one. Then it recomputes the whole receipt and compares it.

## Receipts

The readiness receipt (`projectbuddy/ubuntu-lane-readiness@1`) holds:

| Field | Meaning |
| --- | --- |
| `profile` | the profile's `name` and file `sha256` |
| `vm`, `bootId` | the guest's identity at scan time |
| `runnerAbsence` | `runnerConfigurations`, `runnerUnits`, `skippedRoots`, and `scanErrors` |
| `labviewAbsence` | `paths`, `packages`, and `scanErrors` |
| `toolchain` | for each probe: `name`, `expected`, `observed`, `exitCode`, and `ok` |
| `checkedAt`, `failures`, `outcome` | when the scan ran, every failure, and `PASS` or `FAIL` |

The box receipt (`projectbuddy/ubuntu-lane-box@1`) holds:

| Field | Meaning |
| --- | --- |
| `profile` | the profile's `name` and file `sha256` |
| `source` | the VM's and snapshot's `name` and `uuid` |
| `box` | the box's `filename`, `sha256`, and `sizeBytes` |
| `provenanceSha256` | the SHA-256 of `baseReceipt`, `embeddedVagrantfile`, `profile`, and `readinessReceipt` |
| `os`, `virtualBox` | copied from the profile |
| `toolchain` | the profile's tools, each with the version its probe `observed` |
| `runnerAbsence` | `checkedBootId` and the empty runner findings |
| `labviewAbsence` | the empty LabVIEW findings |
| `sshHostKey` | the guest's SSH host key, from the base receipt |
| `failures`, `outcome` | always `[]` and `PASS`; a failing box gets no receipt |

The box receipt has no timestamp, so that `verify-receipt` can recompute it
exactly.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | `PASS`, or the packaging plan |
| `1` | the readiness outcome is `FAIL`, or a failure after something was changed: a partial toolchain install, or packaging that failed or was interrupted after cloning started, and removed its outputs and clone |
| `2` | a refusal, a failed verification, or a usage error; nothing was changed |
| `127` | `VBoxManage` or `vagrant` is missing, or the given path is not executable |

## Related

- [Ubuntu lane procedure](ubuntu-lane.md): planning a lane run from the box,
  and tearing down a lane instance by VM UUID.
- [VM provisioning procedure](procedure.md): the LabVIEW appliance and its
  receipt chain.
- `VITechnologies/projectbuddy#183`: this lane box profile, packager, and
  receipt.
- `VITechnologies/projectbuddy#185`: the lane plan, teardown, and clone
  verifier that use the box.
- `VITechnologies/projectbuddy#186`: building the lane base VM from a lane
  profile (step 1).
