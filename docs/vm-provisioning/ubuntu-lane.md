# Ubuntu Lane Procedure

- Status: the dry-run lane plan and teardown are built and fixture-tested
  offline (`VITechnologies/projectbuddy#185`); neither is live-proven yet. The
  clone verifier is still to come under the same issue.
- Last substantive update: 2026-11-09
- Owner: svelderrainruiz

The Ubuntu lane runs one disposable VM per job from a verified
[lane box](ubuntu-lane-box.md). The wiki's
[REQ-090](https://github.com/VITechnologies/projectbuddy/wiki/REQ-090) to
[REQ-093](https://github.com/VITechnologies/projectbuddy/wiki/REQ-093) and
[REQ-101](https://github.com/VITechnologies/projectbuddy/wiki/REQ-101) to
[REQ-103](https://github.com/VITechnologies/projectbuddy/wiki/REQ-103) set its
rules. This guide covers the parts that exist so far:

- **The plan** checks every input of one run and writes the run's steps,
  changing nothing.
- **Teardown** removes one lane instance
  ([REQ-102](https://github.com/VITechnologies/projectbuddy/wiki/REQ-102),
  [TEST-104](https://github.com/VITechnologies/projectbuddy/wiki/TEST-104)).

The pieces:

- [`scripts/Invoke-Lane.py`](../../scripts/Invoke-Lane.py): the host command
  line. It needs Python 3.11 or later on a POSIX host.
- [`scripts/lane/`](../../scripts/lane/): the declaration, plan, teardown,
  and receipt modules.
- [`vm/ubuntu-lane/lane-workload.example.json`](../../vm/ubuntu-lane/lane-workload.example.json):
  a generic workload declaration.

## Plan

`Invoke-Lane.py plan` checks every input of one lane run and writes a
`projectbuddy/lane-plan@1` plan. It is a dry run. It runs only read-only
queries: `VBoxManage --version`, `list vms`, and `list systemproperties`;
`vagrant --version`; and `git rev-parse`, `status`, `cat-file`, and
`ls-tree`. It never adds a box, starts or changes a VM, opens a connection,
or writes a secret's value anywhere.

### The workload declaration

A consumer gives its workload as data, in a `projectbuddy/lane-workload@1`
JSON file
([REQ-090](https://github.com/VITechnologies/projectbuddy/wiki/REQ-090)).
The [example](../../vm/ubuntu-lane/lane-workload.example.json) names no
consumer; its commit is a placeholder.

| Field | Meaning |
| --- | --- |
| `schema` | `projectbuddy/lane-workload@1` |
| `commit` | the full 40-digit lowercase candidate commit SHA |
| `workingDirectory` | where the workload runs, in the candidate checkout; `.` is its root |
| `argv` | the one argument vector the workload runs, without a shell |
| `timeoutSeconds` | the workload's limit in the guest, a positive integer |
| `inputs` | the files and directories the workload reads, which must be in the commit |
| `outputs` | the files the run copies into its evidence directory |
| `secrets` | the names of the secrets the workload receives ([REQ-093](https://github.com/VITechnologies/projectbuddy/wiki/REQ-093)) |
| `toolchain` | each tool's `name` and `probe`, as in the [lane profile](ubuntu-lane-box.md#the-lane-profile) ([REQ-091](https://github.com/VITechnologies/projectbuddy/wiki/REQ-091)) |

Every path is relative, uses `/`, and is in normalized form without `..`. An
output is never a VM disk, saved state, VM settings file, or known-hosts
file: a file name ending in `.vdi`, `.vmdk`, `.vhd`, `.vhdx`, `.sav`, or
`.vbox`, or named `known_hosts`, is refused in any letter case.

The declaration's digest is the SHA-256 of its RFC 8785 canonical form, so
the same content has the same digest however the file is formatted. A
declaration with no canonical form is refused: one that holds a `null`, a
number with a fraction or an exponent, an integer beyond 2^53 - 1, a key
that appears twice, or an unpaired surrogate.

Declaration `@1` has one argument vector and no fetch steps. A workload that
needs the network, such as `npm ci`, waits for the fetch phase
(`VITechnologies/projectbuddy#182`).

### Run the plan (host)

```bash
python3 scripts/Invoke-Lane.py plan --declaration "$DECLARATION" \
  --candidate-repo "$CANDIDATE" --box "$BOX" --box-receipt "$BOX_RECEIPT" \
  --profile "$PROFILE" --base-receipt "$BASE_RECEIPT" \
  --readiness-receipt "$READINESS_RECEIPT" --work-dir "$WORK" \
  --evidence-dir "$EVIDENCE" --records-dir "$RECORDS" \
  --plan-out "$OUT/lane-plan.json"
```

- `--declaration` is the workload declaration file.
- `--candidate-repo` is the top level of the host's clone of the candidate
  repository, not a directory inside it. The declared commit must be a commit
  object in it. Every input and the working directory must
  be in that commit's tree, and neither a symbolic link nor a submodule, nor
  below one.
- `--box`, `--box-receipt`, `--profile`, `--base-receipt`, and
  `--readiness-receipt` are the lane box and the files its receipt binds, as
  in [Verify the box](ubuntu-lane-box.md#7-verify-the-box-host).
  `--embedded-vagrantfile` defaults to the engine's
  `vm/ubuntu-lane/Vagrantfile.box`.
- `--work-dir` and `--evidence-dir` hold each run's own run directory and
  evidence directory, named after its instance. `--records-dir` holds the
  teardown receipts of earlier runs, each named
  `<instance name>.teardown.json`. All three must be private (mode 700 or
  stricter).
- `--plan-out` must not exist yet, and its directory must be private. The
  plan and its `.sha256` file are written with mode 600 and hard-linked into
  place, so the directory's file system must support hard links. A plan or
  `.sha256` file that appears while the plan runs is kept, and the plan fails
  (exit code 2).
- `--ssh-port` is the host port for the guest's SSH forward, which listens
  only on `127.0.0.1`. By default the plan picks a free port.
- `--vboxmanage`, `--vagrant`, and `--git` name the tools when they are not
  on `PATH`.

Every path must be absolute. Each plan names a new instance: `pb-lane-` and
12 random hexadecimal digits. The plan records the engine's commit, and
refuses a checkout whose tracked files have changes, because its commit
would not identify the code that runs.

### Refusals

Each failed check is a refusal that names its rule. The plan reports every
refusal it finds, not only the first, and its outcome is `ready` only when
there is none.

| Rule | Refused when |
| --- | --- |
| `declaration-json` | the declaration cannot be read, is not a JSON object, or has no canonical form |
| `declaration-fields` | a field is missing or unknown, or `schema` is not `projectbuddy/lane-workload@1` |
| `commit` | `commit` is not a full 40-digit lowercase SHA: a branch, a tag, or an abbreviated SHA |
| `argv` | `argv` is not a non-empty list of non-empty strings |
| `working-directory` | `workingDirectory` is not a relative path in normalized form |
| `timeout` | `timeoutSeconds` is not a positive integer |
| `input-path` | `inputs` is not a list of strings, or an input is not a relative path below the checkout, or appears twice |
| `output-path` | `outputs` is not a list of strings, or an output is not a relative path below the evidence directory, or appears twice |
| `output-forbidden` | an output is a VM disk, saved state, VM settings file, or known-hosts file |
| `secret-name` | `secrets` is not a list of strings, or a name is not an environment variable name, or appears twice |
| `toolchain` | `toolchain` is not a non-empty list, or an entry is not a valid `name` and `probe`, or a name appears twice |
| `box-digest` | the box's SHA-256 differs from the one its receipt records ([TEST-103](https://github.com/VITechnologies/projectbuddy/wiki/TEST-103)) |
| `box-receipt` | the box or its receipt cannot be read, or the receipt does not verify against the box, profile, base and readiness receipts, and embedded Vagrantfile |
| `toolchain-contract` | the declared toolchain differs from the box receipt's |
| `candidate-missing` | `--candidate-repo` is not the top level of a git clone, or the commit is not a commit object in it (an annotated tag's own SHA is refused) |
| `candidate-path` | an input or the working directory is not in the commit, is or is below a symbolic link or a submodule, or the working directory is not a directory |
| `secret-missing` | a declared secret is unset or empty in the plan's environment |
| `engine-commit` | the engine's commit cannot be read, or its tracked files have changes |
| `earlier-record-invalid` | a `.teardown.json` receipt in the records directory does not verify |
| `reused-instance-uuid` | VirtualBox lists a UUID that an earlier record observed `removed` or `absent` ([REQ-101](https://github.com/VITechnologies/projectbuddy/wiki/REQ-101)) |
| `instance-name-in-use` | the new name is already a registered VM, a run directory, an evidence directory, or a lane record |
| `host-virtualbox` | `VBoxManage --version` fails or prints an unexpected form, `list vms` fails, or VirtualBox names no absolute default machine folder |
| `host-vagrant` | `vagrant --version` fails or prints an unexpected form |
| `host-disk-space` | a file system has less free space than one instance needs there |
| `ssh-port` | the SSH forward's host port is in use |

A plan refused by a declaration rule, `box-digest`, or `box-receipt` has no
steps. The disk check adds up, for each file system, the box file and its
extracted size under the work directory, and the box receipt's full
`diskGb` in VirtualBox's default machine folder, because a dynamic disk
never grows past its capacity.

### The plan

The plan (`projectbuddy/lane-plan@1`) holds:

| Field | Meaning |
| --- | --- |
| `engine` | the engine checkout's `path` and `commit` |
| `instance` | the new instance's `name`, `runDirectory`, and `evidenceDirectory` |
| `declaration` | the declaration's `path`, and its canonical `sha256` or `null` |
| `candidate` | the candidate `repository`, and the declared `commit` or `null` |
| `box` | the box `path` and `sha256`, the `receiptSha256`, the `extractedBytes`, and the receipt's `profile` and `sshHostKey` |
| `host` | the `virtualBox` and `vagrant` versions, the `machineFolder`, and the `disks` the space check compared |
| `hardware` | the box receipt's `virtualBox` block, or `null` |
| `network` | one NAT adapter, its link off during the `build` phase, and the SSH forward from `127.0.0.1` |
| `vagrantfile` | the generated Vagrantfile's `text` and its `sha256` |
| `steps` | the steps below, in order |
| `earlierInstances` | the UUIDs that earlier lane records name |
| `refusals` | each refusal's `rule` and `detail`, in order |
| `outcome` | `ready` or `refused` |

The generated Vagrantfile loads after the box's embedded one. It disables
the synced folder, box updates, linked clones, and Vagrant's removal of
unused network interfaces. It forwards only SSH, from `127.0.0.1`, turns off
the NAT's access to the host's loopback, and sets the box receipt's
hardware. Vagrant does not connect to the guest; the lane does that itself.

Each step has a `name`, a `phase`, where it `runs` (`host`, `guest`, or
`engine`, the lane itself), its `argv`, `cwd`, `environment`, the `secrets`
it receives by name, and its `timeoutSeconds`. `{uuid}` in an argv is the
instance's UUID, which exists only after `vagrant up`. The phases are
`prepare`, `fetch`, `build`, `collect`, and `teardown`; `fetch` has no steps
at declaration `@1`. The plan names these steps; the executor that runs them
is still to come.

| Step | Phase | Runs | What it does |
| --- | --- | --- | --- |
| `box-add` | `prepare` | host | adds the box under the run's own `VAGRANT_HOME`, checked against its SHA-256 |
| `up` | `prepare` | host | `vagrant up` in the run directory |
| `verify-instance` | `prepare` | host | `VBoxManage showvminfo {uuid} --machinereadable` |
| `connect` | `prepare` | engine | connects over SSH, accepting only the host key in the box receipt |
| `probe-<tool>` | `prepare` | guest | runs one declared tool probe, for up to 60 seconds |
| `transfer-candidate` | `prepare` | engine | sends the candidate commit to the guest |
| `workload` | `build` | guest | starts the declared `argv` in the working directory, with only the declared secrets; it waits for the link to go off before it runs |
| `network-off` | `build` | host | `VBoxManage controlvm {uuid} setlinkstate1 off` |
| `verify-network-off` | `build` | host | checks that the link is off |
| `await-workload` | `build` | engine | waits for the workload, for up to its timeout plus 300 seconds |
| `network-on` | `collect` | host | `VBoxManage controlvm {uuid} setlinkstate1 on` |
| `collect-outputs` | `collect` | engine | copies the declared outputs into the evidence directory |
| `write-evidence` | `collect` | engine | writes the run's evidence record |
| `teardown` | `teardown` | host | [teardown](#teardown) with `--run`, writing its receipt to the records directory |

The workload starts before the link goes off because SSH reaches the guest
through the only adapter (`VITechnologies/projectbuddy#182`, option N2).
Host queries time out after 120 seconds, `git` after 120 seconds, and
`box-add` and `up` after 1800 seconds each.

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
  receipt and its `.sha256` file are written with mode 600 and hard-linked
  into place, the same as the plan. A receipt or `.sha256` file that appears
  while teardown runs is kept, and teardown reports that its receipt cannot be
  written.
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
| `0` | a `ready` lane plan, the teardown plan, a `removed` or `absent` teardown, or a receipt that passes |
| `1` | a teardown that was refused or failed after it ran a command, a receipt that could not be written after a command ran, or a consistent receipt that records `refused` or `failed` |
| `2` | a `refused` lane plan, a refusal or failure before any command ran, an invalid receipt, or a usage error; nothing was changed |
| `127` | VBoxManage, Vagrant, or git is missing, or the given path is not executable |

## Related

- [Ubuntu lane box procedure](ubuntu-lane-box.md): building and verifying the
  box that lane instances start from.
- [VM provisioning procedure](procedure.md): the LabVIEW appliances.
- `VITechnologies/projectbuddy#185`: this plan and teardown, and the clone
  verifier.
- `VITechnologies/projectbuddy#182`: the lane network and the fetch phase.
- `VITechnologies/projectbuddy#166`: the lane design.
