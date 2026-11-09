# Ubuntu Lane Procedure

- Status: teardown is built and fixture-tested offline
  (`VITechnologies/projectbuddy#185`); not yet live-proven. The lane plan and
  the clone verifier are still to come under the same issue.
- Last substantive update: 2026-11-09
- Owner: svelderrainruiz

The Ubuntu lane runs one disposable VM per job from a verified
[lane box](ubuntu-lane-box.md). The wiki's
[REQ-101](https://github.com/VITechnologies/projectbuddy/wiki/REQ-101) to
[REQ-103](https://github.com/VITechnologies/projectbuddy/wiki/REQ-103) set its
rules. This guide covers the part that exists so far: **teardown**
([REQ-102](https://github.com/VITechnologies/projectbuddy/wiki/REQ-102),
[TEST-104](https://github.com/VITechnologies/projectbuddy/wiki/TEST-104)).

The pieces:

- [`scripts/Invoke-Lane.py`](../../scripts/Invoke-Lane.py): the host command
  line. It needs Python 3.11 or later on a POSIX host.
- [`scripts/lane/`](../../scripts/lane/): the teardown and receipt modules.

## Teardown

Teardown removes one lane instance, given by its VM UUID. It changes the VM
only through two VBoxManage commands, each addressed by that UUID:

- `VBoxManage controlvm <UUID> poweroff`
- `VBoxManage unregistervm <UUID> --delete`

It then removes the run's directory, checks that the box file is unchanged,
and writes a `projectbuddy/lane-teardown@1` receipt.

### The allow-list

Before each command, teardown reads the VM's state by UUID:

| Observed state | Teardown |
| --- | --- |
| `running`, `paused` | power off, wait to observe the stop, then delete |
| `poweroff`, `aborted` | delete |
| `absent` | nothing to change; the UUID is not in `VBoxManage list vms` |
| any other state | refuse, and leave the VM in place |

Refused states include `livesnapshotting`, `onlinesnapshotting`,
`snapshotting`, `saved`, `stopping`, and any state teardown does not know. A
refused VM needs an operator. Never interrupt a healthy `livesnapshotting`
operation; wait for it to finish, and then run teardown again.

After a poweroff, teardown observes the state every 2 seconds while it is
`running`, `paused`, or `stopping`, for up to 120 seconds. A VM that has not
stopped by then is left in place and the teardown fails. A VM that stops in a
state outside the allow-list, such as `saved`, is refused.

### What teardown guarantees

- **The target is a UUID.** A VM name, a braced or unhyphenated UUID, or
  anything else that is not a canonical UUID is rejected before any query.
  Upper-case UUIDs are accepted and recorded in lower case.
- **The UUID belongs to the run.** The run directory must be a private
  directory (mode 700 or stricter) with no symbolic link on the path to
  Vagrant's machine ID file, `.vagrant/machines/default/virtualbox/id`, and
  that file must name the UUID. Otherwise teardown refuses before any query.
  When the run directory does not exist, teardown changes nothing unless the
  UUID is already absent.
- **The VM is a lane instance.** VirtualBox must report a name matching
  `pb-lane-[0-9a-f]{12}`. The name is a refusal guard only; teardown never
  selects a VM by name.
- **One teardown per UUID at a time.** Teardown holds an exclusive `flock` on
  `<lock dir>/<UUID>.lock` while it observes and changes the VM. A second
  request for the same UUID waits up to 30 minutes, then refuses. When it gets
  the lock after the first request removed the VM, it observes the UUID
  absent and changes nothing.
- **An interrupted delete can be finished.** Once the UUID is absent,
  teardown renames the run directory to `.<run directory name>.teardown-<UUID>`
  beside it, and then deletes that leftover. The rename is atomic, so an
  interruption leaves either the bound run directory or the leftover. A rerun
  that observes the UUID absent deletes the leftover. The leftover must be a
  private directory, not a symbolic link.
- **Removal is observed.** A receipt reports `removed` or `absent` only after
  `VBoxManage list vms` no longer shows the UUID, the run directory and any
  leftover are gone, and the box file's SHA-256 still equals `--box-sha256`.
- **No other VM and no host networking is touched.** Teardown runs only the
  two commands above, each with the instance UUID.

### What it does not guarantee

- **A receipt after an interruption.** If teardown is interrupted (`SIGINT`,
  `SIGTERM`, `SIGKILL`, or a host power loss), it writes no receipt and claims
  nothing. Run it again with a new `--receipt-out`; it starts from the state it
  observes.
- **That a timed-out command did nothing.** VBoxManage commands have
  timeouts: 300 seconds for `controlvm`, 600 seconds for `unregistervm`, and
  120 seconds for each query. On a timeout only the VBoxManage client is
  killed. VirtualBox can still finish the job, and the next teardown observes
  the result.
- **Windows hosts.** The lock uses POSIX `fcntl`.
- **Orphan cleanup.** Nothing yet finds lane VMs that lost their run
  directory. Teardown refuses a present VM when no run directory binds it.
- **A live proof.** No lane instance has been torn down on a real host yet.

### Run teardown (host)

Print the plan first; it validates the inputs, binds the run directory, and
observes the VM, but changes nothing:

```bash
python3 scripts/Invoke-Lane.py teardown --uuid "$VM_UUID" \
  --run-dir "$RUN_DIR" --box "$BOX" --box-sha256 "$BOX_SHA256" \
  --receipt-out "$OUT/lane-teardown.json"
```

Add `--run` to tear down for real. The other inputs:

- `--run-dir` is the run's directory, which holds its Vagrant state. Teardown
  removes it.
- `--box` and `--box-sha256` name the lane box the instance came from, and
  its expected digest from the lane box receipt.
- `--receipt-out` must not exist yet. Its directory must be private. The
  receipt and its `.sha256` file are written with mode 600.
- `--lock-dir` is the private directory for the per-UUID locks. It defaults
  to `$XDG_RUNTIME_DIR/projectbuddy-lane-locks` (`--lock-dir`), created with
  mode 700. Without an absolute `XDG_RUNTIME_DIR`, pass `--lock-dir`. Every
  teardown on a host must use the same lock directory.
- `--vboxmanage` names VBoxManage when it is not on `PATH`.

The box, the receipt, and the lock directory must all be outside the run
directory and its leftover, because teardown deletes both. `--run-dir` must
not contain `..`.

Check a teardown receipt at any time:

```bash
python3 scripts/Invoke-Lane.py verify-teardown "$OUT/lane-teardown.json" \
  --uuid "$VM_UUID" --box-sha256 "$BOX_SHA256"
```

`--uuid` and `--box-sha256` are optional; when given, the receipt must name
that UUID and digest. The verifier checks the `.sha256` file and every rule in
[Receipts](#receipts), and passes only `removed` and `absent` receipts.

### Why not `vagrant destroy`

The lane design (`VITechnologies/projectbuddy#166`) planned
`vagrant destroy -f` from the run directory. Teardown runs VBoxManage
directly instead:

- Vagrant's destroy chain makes its own state decisions. In Vagrant 2.4.9
  (`plugins/providers/virtualbox/action.rb`, `action_destroy` and
  `action_halt`), it discards a `saved` state and resumes a `paused` VM before
  halting. Both bypass the allow-list.
- Vagrant's halt and delete are the same two commands,
  `controlvm <uuid> poweroff` and `unregistervm <uuid> --delete`
  (`driver/version_5_0.rb`). Running them directly puts each command and the
  state observed before it in the receipt.
- The run directory, which holds all of the run's Vagrant state, is deleted
  anyway.
- Vagrant's chain also includes `DestroyUnusedNetworkInterfaces`, which can
  remove host-only interfaces that other VMs use if it is enabled. Teardown
  never runs it.

## Receipts

The teardown receipt (`projectbuddy/lane-teardown@1`) holds:

| Field | Meaning |
| --- | --- |
| `instance` | the target `uuid`, and the `name` VirtualBox reported, or `null` |
| `allowList` | the `poweroff` and `delete` states from [the allow-list](#the-allow-list) |
| `steps` | in order, each `observed` state and each `command` with its `result`: an exit code or `"timeout"` |
| `runDirectory` | `removed` (this teardown deleted the run directory or its leftover), `absent` (there was neither), or `kept` |
| `box` | the `expectedSha256`, and the `sha256AfterTeardown`, which is `null` when teardown did not get that far |
| `failures` | every refusal or failure, in order |
| `outcome` | one of the outcomes below |

| Outcome | Meaning |
| --- | --- |
| `removed` | this teardown deleted the VM, and then observed the UUID absent |
| `absent` | the UUID was absent without a delete by this teardown |
| `refused` | a rule refused the VM, which is left in place |
| `failed` | a command, query, or check failed |

The verifier rejects a receipt when any of these is false:

- `removed` and `absent` end with an observation of `absent`, carry no
  failures, have a removed or absent run directory, and have an unchanged box
  digest. Only `removed` has a successful delete.
- Every command is a poweroff or delete of the instance UUID, follows an
  observed state that the allow-list maps to that command, and is the last
  step if it failed. Nothing follows the absence observation.
- A command was run only against a VM with a lane instance name.
- `refused` and `failed` name at least one failure; `refused` ends with an
  observation of a VM that is still present.
- The receipt has exactly these fields and no credential field.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | the plan, a `removed` or `absent` teardown, or a receipt that passes |
| `1` | a teardown that was refused or failed after it ran a command, a receipt that could not be written after a command ran, or a consistent receipt that records `refused` or `failed` |
| `2` | a refusal or failure before any command ran, an invalid receipt, or a usage error; nothing was changed |
| `127` | VBoxManage is missing, or the given path is not executable |

## Related

- [Ubuntu lane box procedure](ubuntu-lane-box.md): building and verifying the
  box that lane instances start from.
- [VM provisioning procedure](procedure.md): the LabVIEW appliances.
- `VITechnologies/projectbuddy#185`: the lane plan, this teardown, and the
  clone verifier.
- `VITechnologies/projectbuddy#166`: the lane design.
