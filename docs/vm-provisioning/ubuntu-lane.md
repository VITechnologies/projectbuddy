# Ubuntu Lane Procedure

- Status: the dry-run lane plan and teardown are built and fixture-tested
  offline (`VITechnologies/projectbuddy#185`); the plan's fetch and build
  phases and its output disk follow REQ-106
  (`VITechnologies/projectbuddy#200`), and the output disk's format, guest
  launcher, and host reader are fixture-tested
  (`VITechnologies/projectbuddy#202`). The host's SSH channel to the
  instance is fixture-tested and checked against a local `sshd`
  (`VITechnologies/projectbuddy#206`). None is live-proven yet. The clone
  verifier is still to come under `VITechnologies/projectbuddy#185`.
- Last substantive update: 2026-11-11
- Owner: svelderrainruiz

The Ubuntu lane runs one disposable VM per job from a verified
[lane box](ubuntu-lane-box.md). The wiki's
[REQ-090](https://github.com/VITechnologies/projectbuddy/wiki/REQ-090) to
[REQ-093](https://github.com/VITechnologies/projectbuddy/wiki/REQ-093) and
[REQ-101](https://github.com/VITechnologies/projectbuddy/wiki/REQ-101) to
[REQ-103](https://github.com/VITechnologies/projectbuddy/wiki/REQ-103), and
[REQ-106](https://github.com/VITechnologies/projectbuddy/wiki/REQ-106) set its
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
  and receipt modules, the host's SSH channel, the guest supervisor, and the
  output disk's guest launcher and host reader.
- [`vm/ubuntu-lane/lane-workload.example.json`](../../vm/ubuntu-lane/lane-workload.example.json):
  a generic workload declaration.

## Plan

`Invoke-Lane.py plan` checks every input of one lane run and writes a
`projectbuddy/lane-plan@2` plan. It is a dry run. It runs only read-only
queries: `VBoxManage --version`, `list vms`, and `list systemproperties`;
`vagrant --version`; and `git rev-parse`, `status`, `cat-file`, and
`ls-tree`. It never adds a box, starts or changes a VM, opens a connection,
or writes a secret's value anywhere.

### The workload declaration

A consumer gives its workload as data, in a `projectbuddy/lane-workload@2`
JSON file
([REQ-090](https://github.com/VITechnologies/projectbuddy/wiki/REQ-090)).
The [example](../../vm/ubuntu-lane/lane-workload.example.json) names no
consumer; its commit is a placeholder.

| Field | Meaning |
| --- | --- |
| `schema` | `projectbuddy/lane-workload@2` |
| `commit` | the full 40-digit lowercase candidate commit SHA |
| `workingDirectory` | where every step runs, in the candidate checkout; `.` is its root |
| `fetch` | the fetch phase: its `steps`, which may be none, and its `timeoutSeconds`; it has the network and no secrets |
| `build` | the build phase: at least one step, and its `timeoutSeconds`; it runs with the link off and receives the secrets ([REQ-106](https://github.com/VITechnologies/projectbuddy/wiki/REQ-106)) |
| `inputs` | the files and directories the workload reads, which must be in the commit |
| `outputs` | the files, below the working directory, that the guest leaves on the [output disk](#the-output-disk) and the run copies into its evidence directory |
| `maxOutputBytes` | the most bytes the guest may write to the output disk, an integer of at least 2048 |
| `secrets` | the names of the secrets the build phase receives ([REQ-093](https://github.com/VITechnologies/projectbuddy/wiki/REQ-093)) |
| `toolchain` | each tool's `name` and `probe`, as in the [lane profile](ubuntu-lane-box.md#the-lane-profile) ([REQ-091](https://github.com/VITechnologies/projectbuddy/wiki/REQ-091)) |

A phase's `steps` is a list of argument vectors, each a non-empty list of
non-empty strings that runs without a shell. Its `timeoutSeconds`, a positive
integer, is the guest's limit for all of the phase's steps together.

Every path is relative, uses `/`, and is in normalized form without `..`. An
output's path is at most 100 bytes in UTF-8, and no output is below another
output. An output is never a VM disk, saved state, VM settings file, or known-hosts
file: a file name ending in `.vdi`, `.vmdk`, `.vhd`, `.vhdx`, `.sav`, or
`.vbox`, or named `known_hosts`, is refused in any letter case.

The declaration's digest is the SHA-256 of its RFC 8785 canonical form, so
the same content has the same digest however the file is formatted. A
declaration with no canonical form is refused: one that holds a `null`, a
number with a fraction or an exponent, an integer beyond 2^53 - 1, a key
that appears twice, or an unpaired surrogate.

Declaration `@1`, which had one argument vector and no fetch phase, is
refused: it draws no line between the steps that need the network and the
steps that receive secrets. A workload such as an npm package fetches with
`npm ci --ignore-scripts` and then runs `npm rebuild` and its own scripts in
the build phase, as the example does.

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
  in [Verify the box](ubuntu-lane-box.md#8-verify-the-box-host).
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
| `declaration-fields` | a field is missing or unknown, or `schema` is not `projectbuddy/lane-workload@2`; `@1` is refused as retired |
| `commit` | `commit` is not a full 40-digit lowercase SHA: a branch, a tag, or an abbreviated SHA |
| `phase` | `fetch` or `build` is not an object with exactly `steps` and `timeoutSeconds`, its `steps` is not a list, or `build` has no step |
| `argv` | a step is not a non-empty list of non-empty strings |
| `timeout` | a phase's `timeoutSeconds` is not a positive integer |
| `working-directory` | `workingDirectory` is not a relative path in normalized form |
| `input-path` | `inputs` is not a list of strings, or an input is not a relative path below the checkout, or appears twice |
| `output-path` | `outputs` is not a list of strings, or an output is not a relative path below the working directory, is longer than 100 bytes in UTF-8, is below another output, or appears twice |
| `output-forbidden` | an output is a VM disk, saved state, VM settings file, or known-hosts file |
| `output-size` | `maxOutputBytes` is not an integer of at least 2048, the smallest archive the output disk holds |
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
| `run-directory-path` | the run directory, `<work directory>/<instance name>`, does not match `/[A-Za-z0-9._/@+-]*`, so ssh would expand or split it ([the SSH channel](#the-ssh-channel)) |

A plan refused by a declaration rule, `box-digest`, or `box-receipt` has no
steps. The disk check adds up, for each file system, the box file, its
extracted size, and the output disk under the work directory; the box
receipt's full `diskGb` in VirtualBox's default machine folder, because a
dynamic disk never grows past its capacity; and `maxOutputBytes` under the
evidence directory.

### The plan

The plan (`projectbuddy/lane-plan@2`) holds:

| Field | Meaning |
| --- | --- |
| `engine` | the engine checkout's `path` and `commit` |
| `instance` | the new instance's `name`, `runDirectory`, and `evidenceDirectory` |
| `declaration` | the declaration's `path`, and its canonical `sha256` or `null` |
| `candidate` | the candidate `repository`, and the declared `commit` or `null` |
| `box` | the box `path` and `sha256`, the `receiptSha256`, the `extractedBytes`, and the receipt's `profile` and `sshHostKey` |
| `host` | the `virtualBox` and `vagrant` versions, the `machineFolder`, and the `disks` the space check compared |
| `hardware` | the box receipt's `virtualBox` block, or `null` |
| `network` | one NAT adapter, its link off from the `build` phase on and never turned on again, and the SSH forward from `127.0.0.1` |
| `outputDisk` | the output disk's `path` in the run directory, its `sizeMb`, `format`, and `variant`, and the `controller`, `port`, and `device` it is attached at, or `null` |
| `vagrantfile` | the generated Vagrantfile's `text` and its `sha256` |
| `guestUnits` | for `fetch` and `build`, the declared `timeoutSeconds` after which the guest stops that phase's unit, or `null` |
| `steps` | the steps below, in order |
| `earlierInstances` | the UUIDs that earlier lane records name |
| `refusals` | each refusal's `rule` and `detail`, in order |
| `outcome` | `ready` or `refused` |

The generated Vagrantfile loads after the box's embedded one. It disables
the synced folder, box updates, linked clones, and Vagrant's removal of
unused network interfaces. It forwards only SSH, from `127.0.0.1`, turns off
the NAT's access to the host's loopback, and sets the box receipt's
hardware. Vagrant does not connect to the guest; the lane does that itself.

Before the guest first boots, the Vagrantfile creates the output disk, a
fixed-size VDI of `maxOutputBytes` rounded up to whole MiB, in the run
directory, and attaches it to the `SATA` controller at port 1. The lane
box's VM has that controller with two ports and its system disk on port 0. A
box without that port fails `vagrant up` before the guest boots. The plan
itself creates no disk.

Before the guest first boots, the Vagrantfile also sets the run's public key,
read from `client-key.pub` in the run directory, as the guest property
`/VirtualBox/HostInfo/ProjectBuddy/Lane/ClientKey` with the flag
`RDONLYGUEST`. The guest cannot set a `/VirtualBox/HostInfo/` property, and
the flag makes it read-only to the guest. The box's lane access service makes
that key root's only authorized key
([lane access](ubuntu-lane-box.md#3-configure-lane-access-guest-root)). Vagrant
reads the key file when it reads the Vagrantfile, after `client-key` has
written it, so the plan's Vagrantfile, and its `sha256`, names the file and
not the key.

Each step has a `name`, a `phase`, where it `runs` (`host`, `guest`, or
`engine`, the lane itself), its `argv`, `cwd`, `environment`, the `secrets`
it receives by name, and its `timeoutSeconds`. `{uuid}` in an argv is the
instance's UUID, which exists only after `vagrant up`. The phases are
`prepare`, `fetch`, `build`, `collect`, and `teardown`; `fetch` has no steps
when the declaration's fetch phase has none. A phase's guest steps run in
order, in one unit that the guest stops after that phase's `timeoutSeconds`
in `guestUnits`; a guest step's own `timeoutSeconds` is `null`.
The plan names these steps; the executor that runs them, on the host and in
the guest, is still to come, apart from the [SSH channel](#the-ssh-channel),
the [workload placement](#workload-placement) steps, the
[guest supervisor](#guest-phases), and the output disk's launcher and reader.

| Step | Phase | Runs | What it does |
| --- | --- | --- | --- |
| `client-key` | `prepare` | engine | generates the run's `ssh-ed25519` key pair, `client-key` and `client-key.pub`, in the run directory, without a passphrase and commented with the instance's name |
| `box-add` | `prepare` | host | adds the box under the run's own `VAGRANT_HOME`, checked against its SHA-256 |
| `up` | `prepare` | host | `vagrant up` in the run directory |
| `verify-instance` | `prepare` | host | `VBoxManage showvminfo {uuid} --machinereadable` |
| `connect` | `prepare` | engine | records the instance's host key only when its fingerprint is the box receipt's, then logs in as root with only the run's key ([the SSH channel](#the-ssh-channel)) |
| `check-runner-units` | `prepare` | engine | lists the guest's units with `systemctl list-unit-files` and `systemctl list-units --all`, and stops the run if any is an `actions.runner.*` unit ([REQ-103](https://github.com/VITechnologies/projectbuddy/wiki/REQ-103), [clone check](ubuntu-vagrant-clone.md#runner-units)) |
| `create-account` | `prepare` | engine | creates the workload account, `pb-workload`, without a login shell, and denies it cron and at ([workload placement](#workload-placement)) |
| `probe-<tool>` | `prepare` | guest | runs one declared tool probe as the workload account, for up to 60 seconds |
| `transfer-candidate` | `prepare` | engine | writes the candidate commit to the run directory as `candidate.tar` and extracts it in the guest as the workload account |
| `place-guest-files` | `prepare` | engine | places the supervisor, the launcher, and the run's spec in `/run/projectbuddy-lane` |
| `fetch-<n>` | `fetch` | guest | runs fetch step `n` in the working directory, with the network and no secrets |
| `await-fetch` | `fetch` | engine | runs the supervisor's `fetch` mode, which waits for the fetch steps to end, for up to the fetch timeout plus 300 seconds; there is none when the fetch phase has no step |
| `place-secrets` | `build` | engine | places the declared secrets, which it receives by name, as root's own file in `/run/projectbuddy-lane` |
| `start-build` | `build` | engine | runs the supervisor's `start-build` mode while the link is still on |
| `build-<n>` | `build` | guest | runs build step `n` in the working directory, with only the declared secrets; the build waits for the link to go off before its first step runs |
| `network-off` | `build` | host | `VBoxManage controlvm {uuid} setlinkstate1 off` |
| `verify-network-off` | `build` | host | checks that the link is off |
| `await-poweroff` | `build` | engine | waits for the guest to leave its outputs on the output disk and power itself off, for up to the build timeout plus 300 seconds; after that the run timed out, no output is read, and teardown powers the instance off |
| `read-outputs` | `collect` | host | `VBoxManage mediumio --disk=<run directory>/lane-output.vdi cat --output=-` streams the output disk without booting the guest; the lane checks it and copies the declared outputs into the evidence directory, and anything else on the disk fails the run ([the output disk](#the-output-disk)) |
| `write-evidence` | `collect` | engine | writes the run's evidence record |
| `teardown` | `teardown` | host | [teardown](#teardown) with `--run`, writing its receipt to the records directory |

The build starts before the link goes off because SSH reaches the guest
through the only adapter (`VITechnologies/projectbuddy#182`, option N2). The
link is never turned on again
([REQ-106](https://github.com/VITechnologies/projectbuddy/wiki/REQ-106),
completion channel C1): any guest process can set a VirtualBox guest
property, so nothing the guest reports could safely reopen it. The guest
therefore leaves its outputs on the output disk and powers itself off, and
no secret-bearing process ever has the network.
Host queries time out after 120 seconds, `git` after 120 seconds, and
`box-add`, `up`, and `read-outputs` after 1800 seconds each.

### The SSH channel

The SSH channel
([`scripts/lane/channel.py`](../../scripts/lane/channel.py)) runs the
`client-key`, `connect`, and `check-runner-units` steps, and every later
command the engine runs in the guest. It reaches the instance only through
the SSH forward on `127.0.0.1`.

Every clone of a lane box has the box's SSH host key, so the host key alone
does not tell instances apart. The instance is bound by the client key
instead: only the instance whose host-reserved property holds this run's
public key accepts this run's private key, and only the host can set that
property. A session that logs in therefore reaches this run's instance and no
other.

`client-key` refuses a run directory that is not an absolute directory whose
group and others have no access, or that already has either key file. It runs
`ssh-keygen -q -t ed25519 -N "" -C <instance name> -f <run directory>/client-key`,
for up to 60 seconds, and accepts the result only when the private key is a
regular file with one link, owned by the lane's account, that gives its
group and others no access, as ssh itself requires, and the public key is one `ssh-ed25519` key commented with the
instance's name. Teardown deletes the run directory, and the key with it.

`connect` first records the instance's host key. It refuses a run directory
that already has a `known_hosts` file, then runs
[`enroll-ssh-host-key.py`](../../vm/ubuntu-labview/enroll-ssh-host-key.py)
against the forward with the box receipt's fingerprint, up to 30 times, 5
seconds apart, for up to 20 seconds each, until it succeeds. The resulting
`known_hosts` must hold exactly one line, `[127.0.0.1]:<port> ssh-ed25519
<key>`, whose fingerprint is the box receipt's. `connect` then runs `id -u`
as root, retrying up to 12 times, 5 seconds apart, while ssh itself fails
with exit code 255, because the guest's lane access service runs before
`ssh.service`. The run continues only when `id -u` exits 0 and prints `0`.

Every guest command runs on the host without a shell, as:

```text
ssh -F none -p <port> -o IdentityFile=<run directory>/client-key -o UserKnownHostsFile=<run directory>/known_hosts -o BatchMode=yes -o IdentitiesOnly=yes -o IdentityAgent=none -o StrictHostKeyChecking=yes -o GlobalKnownHostsFile=/dev/null -o HostKeyAlgorithms=ssh-ed25519 -o UpdateHostKeys=no -o CheckHostIP=no -o PasswordAuthentication=no -o KbdInteractiveAuthentication=no -o ControlMaster=no -o ControlPath=none -o ForwardAgent=no -o ForwardX11=no -o ClearAllForwardings=yes -o RequestTTY=no -o ConnectTimeout=10 -- root@127.0.0.1 <command>
```

`<command>` is the command's words quoted for a POSIX shell and joined into
one string, which root's shell in the guest reads; a command with no word, or
with an empty word, is refused. The choices that matter:

- `-F none` ignores every ssh configuration file, and `-o IdentityFile=`
  names the run's key. ssh drops a `-i` file that it cannot read and then
  offers `~/.ssh`'s default keys; with `IdentitiesOnly=yes` and the option
  form it offers only the run's key.
- `--` ends the options. ssh reads options after the destination too, so
  without it a command could be read as one.
- ssh expands `%` tokens and a leading `~` in `IdentityFile` and
  `UserKnownHostsFile`, and splits an option's value at whitespace and
  quotes. The `run-directory-path` rule therefore refuses any run directory
  that does not match `/[A-Za-z0-9._/@+-]*`.

`connect`'s login and `check-runner-units`' queries time out after 60
seconds each.

### Workload placement

The workload placement steps
([`scripts/lane/placement.py`](../../scripts/lane/placement.py)) run as root
over the [SSH channel](#the-ssh-channel). Each step requires every command it
runs to exit 0 and the guest to end in the state below; anything else stops
the run. Each guest command they run times out after 60 seconds unless
this section gives another timeout.

`create-account` requires `getent passwd pb-workload` to exit 2, which means
the guest has no such account, and then runs:

```text
useradd --create-home --home-dir /home/pb-workload --shell /usr/sbin/nologin --user-group pb-workload
```

It accepts the account only when `getent passwd pb-workload` then prints
`pb-workload:x:<uid>:<gid>::/home/pb-workload:/usr/sbin/nologin`, with a user
ID and a group ID other than 0. It appends `pb-workload` to `/etc/cron.deny`
and `/etc/at.deny` with `tee --append`, and checks each file with
`grep --fixed-strings --line-regexp`. When the guest has neither
`/etc/at.allow` nor `/etc/at.deny`, only root may use `at`, so creating
`at.deny` lets every other account use it; the lane accepts that residual
risk.

Each `probe-<tool>` runs `runuser -u pb-workload -- <argv>`, for up to 60
seconds. As the box receipt's probe does, it requires the probe's `pattern`
to match the whole trimmed output and its group to be the declared
`expected` version, so the build uses the tools that the workload account
itself finds.

`transfer-candidate` writes the candidate commit's tree to `candidate.tar` in
the run directory, as a new file with mode 600, and records its SHA-256. It
builds the archive itself from `git ls-tree -r -t -z --full-tree` and
`git cat-file --batch`, not with `git archive`, which applies the tree's
`export-ignore` and `export-subst` attributes and has no option to turn them
off. git runs with `--no-replace-objects` and without the caller's `GIT_*`
variables, so neither a replace ref nor the environment changes what it
reads. The archive is deterministic: PAX format, owner and group 0 with no
names, modification time 0, and mode 0644 or 0755 for a file, by its
executable bit, 0755 for a directory, and 0777 for a symbolic link, which
stays a link. A submodule, or a path or link target that is not UTF-8, stops
the run. In the guest, the workload account creates its candidate directory,
which must not exist yet, and extracts the archive from standard input, for
up to 1800 seconds:

```text
runuser -u pb-workload -- mkdir -m 0700 -- /home/pb-workload/candidate
runuser -u pb-workload -- tar --extract --file=- --directory=/home/pb-workload/candidate --no-same-owner --no-same-permissions --touch
```

`--no-same-permissions` applies the account's umask to the archive's modes,
and `--touch` gives each file the extraction time, as a checkout does. The
spec's `root` is the declared working directory in
`/home/pb-workload/candidate`.

`place-guest-files` takes the output disk from `verify-instance`'s output:
the slot the plan attaches the disk at, `SATA-1-0`, must hold the plan's
disk, and the instance must have no snapshot, since a snapshot would put a
differencing image in that slot. The medium in `SATA-ImageUUID-1-0` gives the
spec's `device` ([the output disk](#the-output-disk)). The engine builds the
spec from the declaration and refuses it unless the supervisor's own check
accepts it. It then runs `mkdir -m 0755 -- /run/projectbuddy-lane`, which
fails when the directory exists, and places this engine's `supervisor.py`
and `output_disk.py` and the spec, each sent on standard input to:

```text
install -m 0644 -o root -g root /dev/stdin /run/projectbuddy-lane/<file>
```

It accepts them only when `stat --printf='%u %g %f %h %s\n'` shows the
directory as root's with mode 0755, and each file as root's with mode 0644,
one link, and the size sent, and `sha256sum` prints the SHA-256 of what it
sent.

`await-fetch` runs `/usr/bin/python3 /run/projectbuddy-lane/supervisor.py fetch`
for up to the fetch timeout plus 300 seconds, and requires exit 0 and the
output `fetch: exited 0`.

`place-secrets` sends one `NAME=VALUE` followed by a NUL byte for each
declared secret, in declared order, on standard input to:

```text
sh -c 'umask 077 && exec install -m 0600 -o root -g root /dev/stdin /run/projectbuddy-lane/secrets'
```

A value travels only on standard input, never in an argv, and no message
holds one; a missing or empty value, or one with a NUL byte, stops the run
and names only the secret. With no declared secret the file is empty, since
the build unit still reads it as its standard input. The engine accepts the
file only when `stat` shows it as root's with mode 0600, one link, and the
size sent. The step runs last before `start-build`, so the secrets file is in
the guest only from then until the build unit opens it.

`start-build` runs
`/usr/bin/python3 /run/projectbuddy-lane/supervisor.py start-build` while the
link is on, for up to 180 seconds, and requires exit 0 and the output
`start-build: pb-lane-supervisor.service started`.

### Guest phases

The guest supervisor
([`scripts/lane/supervisor.py`](../../scripts/lane/supervisor.py)) runs the
`fetch` and `build` phases in the guest and hands off to the output disk's
launcher ([REQ-106](https://github.com/VITechnologies/projectbuddy/wiki/REQ-106)).
Like the launcher, it is one file that uses only the Python standard library.
The engine places both files in `/run/projectbuddy-lane`, which root owns
with mode 0755, beside the run's spec, `lane-guest.json`, with mode 0644, so
the workload account can read them but not change them. Only root can create
an entry in `/run`, so the workload account cannot claim that directory
first. Outside a lane guest every mode changes nothing and exits 2. The
engine runs these modes as root over the [SSH channel](#the-ssh-channel), in
the [workload placement](#workload-placement) steps, which also write the spec
and the secrets file.

| Mode | Runs as | What it does |
| --- | --- | --- |
| `fetch` | root, over SSH | starts `pb-lane-fetch.service`, waits until it ends, empties its control group, and prints `fetch: <result>`; it exits 0 only when the result is `exited 0` |
| `start-build` | root, over SSH while the link is on | makes every check that does not need carrier loss, then starts the `build` mode in `pb-lane-supervisor.service`, which outlives the SSH connection |
| `build` | root, in `pb-lane-supervisor.service` | waits for carrier loss, starts `pb-lane-build.service` with the secrets on its standard input, deletes the secrets file, waits until the unit ends, empties its control group, and replaces itself with the launcher; any failure before that powers the guest off without writing, so the host refuses the disk |
| `phase` | the workload account | runs one phase's steps in order, as that phase's unit; it refuses to run as root |

The spec (`projectbuddy/lane-guest@1`) holds exactly these fields, and a key
that repeats refuses it:

| Field | Meaning |
| --- | --- |
| `schema` | `projectbuddy/lane-guest@1` |
| `account` | the workload account, which must exist and must not be root |
| `root` | the working directory, an absolute path without empty, `.`, or `..` components |
| `fetch` | the fetch phase's `steps`, each a nonempty argument vector, and its `timeoutSeconds`; the `fetch` mode refuses a spec without a fetch step |
| `build` | the build phase's `steps`, at least one, and its `timeoutSeconds` |
| `secrets` | the distinct environment variable names the build steps receive |
| `outputs` | the declared outputs, for the launcher |
| `maxOutputBytes` | the output disk's size, for the launcher |
| `device` | the output disk's `/dev/disk/by-id` path |

Each phase runs in its own transient unit in `system.slice`, as the
workload account. The build unit is started with:

```text
systemd-run --unit=pb-lane-build.service --slice=system.slice \
  --service-type=exec --uid=<uid> --working-directory=<root> \
  --expand-environment=no --quiet --property=RemainAfterExit=yes \
  --property=RuntimeMaxSec=<timeoutSeconds> --property=TimeoutStopSec=30 \
  --property=NoNewPrivileges=yes --property=LimitCORE=0 \
  --property=StandardInput=file:/run/projectbuddy-lane/secrets \
  -- /usr/bin/python3 /run/projectbuddy-lane/supervisor.py phase build
```

The fetch unit differs only in its name, `pb-lane-fetch.service`, its own
phase's `timeoutSeconds`, `StandardInput=null`, and `phase fetch`. Each
property has one purpose:

- `--service-type=exec` makes `systemd-run` return only after systemd has
  started the phase runner, so after it has opened the standard input. It
  opens it as root, before it changes to the workload account, so the
  secrets file can stay closed to that account, and the supervisor deletes
  it as soon as `systemd-run` returns.
- `RemainAfterExit=yes` keeps a unit whose runner ended cleanly loaded, as
  `active` and `exited`. systemd counts an exit 0 as clean, and also a death
  by `SIGHUP`, `SIGINT`, `SIGTERM`, or `SIGPIPE`. A nonzero exit, any other
  signal, or the timeout leaves the unit `failed`, which systemd also keeps.
  Either way the launcher can read the result, which it takes from how the
  runner ended, not from whether systemd counted that clean.
- `RuntimeMaxSec` is the phase's `timeoutSeconds`. When it passes, systemd
  stops the unit with the result `timeout`, and kills what is left after
  `TimeoutStopSec`, 30 seconds.
- `NoNewPrivileges=yes` means no step gains privileges, through `sudo` or
  any other setuid program.
- `LimitCORE=0` sets every step's core file size limit to 0, so the kernel
  writes no core file of a step's memory, which holds the secrets. When the
  kernel's `core_pattern` pipes to a crash handler instead, that handler
  decides whether to keep a copy; either way the copy stays in the guest,
  which teardown deletes.
- `--expand-environment=no` keeps systemd from expanding `$` in the
  command, and `--working-directory` starts every step in `root`.

The secrets never reach a unit through `StandardInputData=`, which any
local account can read as a unit property, or through `systemd-run --pipe`,
which does not wait for the unit to start. A step's standard output and
error go to the journal.

The phase runner, the `phase` mode:

1. reads its whole standard input and closes it. That payload is one
   `NAME=VALUE` followed by a NUL byte for each declared secret, in declared
   order, and nothing else; the fetch unit's payload is empty. A payload that
   does not match refuses before any step, and the refusal never holds a
   value;
2. runs each step with no shell, its standard input from `/dev/null`, and
   its own environment plus the secrets;
3. stops at the first step that does not exit 0 and exits with that step's
   code. A step it cannot find exits 127, and a step it cannot run for any
   other reason exits 126;
4. when a step is killed by a signal, kills itself with the same signal, so
   the unit's result is `killed <signal>`.

As root, the supervisor:

- requires the unified cgroup v2 hierarchy, `/sys/fs/cgroup/cgroup.controllers`;
- waits until a unit is `failed`, or `active` and `exited`. A unit in any
  other state than `active` or `deactivating` has lost its result. It gives
  up when the phase's `timeoutSeconds` plus 30 plus 60 seconds have passed;
- then always empties the unit's control group: it repeats
  `systemctl kill --kill-whom=all --signal=SIGKILL -- <unit>` until
  `/sys/fs/cgroup/system.slice/<unit>/cgroup.events` holds the line
  `populated 0` or the group is gone, for up to 30 seconds. The kernel
  refuses to remove a control group that holds a process, so a missing one
  is empty. No process that a step left behind runs beside the next phase
  or the launcher;
- finds the guest's network interface as the only entry in
  `/sys/class/net` that has a `device` link, and refuses unless there is
  exactly one. The carrier is lost only when that interface's `carrier`
  reads `0`; any other value or a read error means it may still be up. The
  `build` mode waits up to 300 seconds for that, because the build's own
  timeout cannot cover a wait before the build starts;
- checks that the secrets file, `/run/projectbuddy-lane/secrets`, which the
  engine writes while the link is on and leaves empty when no secret is
  declared, is root's own regular file with one link and no group or other
  access, and holds exactly the declared names. Both `start-build` and
  `build` check it, and `build` deletes it on every path.

Once the build unit's control group is empty, `build` replaces itself with
the launcher, with one `--output` for each declared output:

```text
/usr/bin/python3 /run/projectbuddy-lane/output_disk.py --device=<device> \
  --root=<root> --uid=<uid> --unit=pb-lane-build.service \
  --max-output-bytes=<maxOutputBytes> --output=<output>
```

The output disk's `device` is `/dev/disk/by-id/ata-VBOX_HARDDISK_VB`, then
the first eight hex digits of the disk's medium UUID, a `-`, and the UUID's
last four bytes in reverse order, because VirtualBox's AHCI controller
reports that serial. For the medium UUID
`aabbccdd-eeff-0011-2233-445566778899` it is
`/dev/disk/by-id/ata-VBOX_HARDDISK_VBaabbccdd-99887766`. The launcher still
checks that it is a block device of exactly the output disk's size.

A fetch step can schedule work through another service, such as `cron` or
`at`, and that work runs outside the fetch unit's control group, so emptying
the group does not remove it. It has no network during the build, so, like
any fetch step, it can only change what a build step later sees. Turning
such services off for the workload account belongs to the lane box's
readiness check.

### The output disk

The output disk carries the build's result and outputs from the guest, which
has no network after the build starts, to the host
([REQ-106](https://github.com/VITechnologies/projectbuddy/wiki/REQ-106),
completion channel C1). It holds one POSIX ustar archive from byte 0, with no
file system, and zeros after it. The archive's first member,
`./build-result`, is one line: `exited <code>` (0 to 255), `killed <signal>`
(1 to 64), or `timeout`, the build unit's own timeout, which is distinct from
a nonzero exit
([REQ-090](https://github.com/VITechnologies/projectbuddy/wiki/REQ-090)).
The declared outputs that exist follow in declared order, each under its
declared path. Every member is a regular file with mode 0600, owner and
group 0, modification time 0, and no user or group name, and the archive
ends with two zero blocks.

The launcher
([`scripts/lane/output_disk.py`](../../scripts/lane/output_disk.py)) is one
file that uses only the Python standard library, so the engine can copy it
into the guest and run it with the `python3` that the
[base bootstrap](ubuntu-lane-box.md) installs. It runs as root after the
build unit has ended and its control group is empty, and it:

1. changes nothing and exits 2 unless
   `/var/lib/projectbuddy/base-bootstrap-state.json`, which the lane box's
   readiness check requires, exists, so it cannot write a disk or power off
   a machine that is not a lane guest;
2. reads the build unit's `Result`, `ExecMainCode`, and `ExecMainStatus`
   with `systemctl show`, so the unit must stay loaded after it ends; when
   they do not give one result line, it writes no archive;
3. opens the output device only if it is a block device of exactly the output
   disk's size;
4. opens each declared output below the working directory without following
   a symbolic link in any path component, and keeps it only if it is a
   regular file that the workload account owns and that has one link; a
   build step therefore cannot make root read a file such as a host key;
5. writes the result and the outputs it kept, or the result alone when they
   would end past `maxOutputBytes`, then zeros to the end of the device, and
   flushes it;
6. writes the first header last and flushes it again: until then block 0 is
   zero, so the host refuses a disk that the launcher left partway;
7. always powers the guest off, even when its arguments are malformed or a
   step above failed. Malformed arguments, an invalid unit name or
   `maxOutputBytes`, and a device that fails step 3 make it exit 1 before
   anything is written; a write that fails before the first header is
   complete leaves a disk that the host refuses.

The host reads the disk only after it observes the instance `poweroff`. It
streams the disk with `mediumio cat`, which opens it read-only and does not
register it again, and checks every byte; the read fails on the first broken
rule and keeps no output. A refusal names a member by its position, never by
a name the guest wrote.

| Rule | The read fails when |
| --- | --- |
| `disk-length` | the stream holds fewer or more bytes than the output disk's size; `mediumio cat` can stop early and still exit 0 |
| `header` | a header is not byte for byte the one the launcher writes for its name and size |
| `member` | the first member is not `./build-result`, or a later one is not a declared output that follows the previous member in declared order |
| `padding` | a byte between a member's data and the next block is not zero |
| `length` | the archive does not end within `maxOutputBytes` |
| `end` | a zero block is not followed by a second one |
| `tail` | a byte after the archive's end is not zero |
| `build-result` | the disk holds no `./build-result`, it is longer than 16 bytes, or it is not one result line |
| `output-missing` | a declared output is not on the disk; the read keeps the build result |

A disk that breaks any other rule has no build result. How the run's status
in the evidence record follows from the build result is still to come with
the executor.

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
- **An output disk that was never attached.** `vagrant up` creates the output
  disk and then attaches it. If the attach fails, VirtualBox keeps the disk in
  its media registry with no VM: `unregistervm --delete` does not delete it,
  and removing the run directory leaves the registry naming a missing file.
  Close it with `VBoxManage closemedium disk <path>`.
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
