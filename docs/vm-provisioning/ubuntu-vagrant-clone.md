# Ubuntu Vagrant Clone Check

- Status: built and fixture-tested offline (`VITechnologies/projectbuddy#185`);
  the `@2` receipt is not yet live-proven.
- Last substantive update: 2026-11-09
- Owner: svelderrainruiz

The clone check powers on nothing itself. It checks a clone that Vagrant has
already started from a packaged box, runs a capture in it, writes a receipt,
and powers the clone off. It has two parts:

- A neutral core in [`scripts/vagrant_clone/`](../../scripts/vagrant_clone/).
  It binds the clone's VirtualBox UUID, name, `running` state, Vagrant machine
  ID, and single loopback SSH forward, enrolls only the expected SSH host key,
  and checks for Actions runner units
  ([REQ-103](https://github.com/VITechnologies/projectbuddy/wiki/REQ-103)).
  It knows nothing about LabVIEW.
- A clone profile, `projectbuddy/ubuntu-vagrant-clone-profile@1`, that says
  what the clone must show, as data. The LabVIEW appliance's profile is
  [`vm/ubuntu-labview/clone-profile.json`](../../vm/ubuntu-labview/clone-profile.json),
  next to the appliance's
  [manifest](../../vm/ubuntu-labview/provisioning-manifest.json).

The appliance keeps its two commands,
[`verify-vagrant-clone.py`](../../vm/ubuntu-labview/verify-vagrant-clone.py)
and
[`verify-vagrant-clone-receipt.py`](../../vm/ubuntu-labview/verify-vagrant-clone-receipt.py),
with the same arguments as before plus `--profile`, which defaults to the
`clone-profile.json` beside them. The checks that only the appliance needs stay
in `verify-vagrant-clone.py`: the evidence directory must be on internal NVMe,
the expected host key comes from the base receipt that the box receipt binds,
and the box receipt is verified again with `verify-vagrant-box-receipt.py`.

The lane plan's `check-runner-units` step names the same runner unit check
(see [the Ubuntu lane procedure](ubuntu-lane.md#the-plan)); the lane executor
that will run it is still to come.

## The clone profile

A profile is a JSON object with exactly these fields, plus the optional
`receiptV1Runtime`:

| Field | Meaning |
| --- | --- |
| `schema` | `projectbuddy/ubuntu-vagrant-clone-profile@1` |
| `name` | the profile's name, matching `[a-z0-9][a-z0-9._+-]*` |
| `capture` | the capture's `script`, `timeoutSeconds`, and `files` |
| `bootId` | the `file` and `pointer` of the guest's boot ID, and `alsoIn`, the other places that must record the same boot ID |
| `checks` | what each evidence file must contain |
| `runnerUnits` | `refuse` or `record` (see [Runner units](#runner-units)) |

`capture.script` is a file name in the profile's directory. The check copies
it into the guest and runs it as `<script> --output-dir <directory>`, for up to
`timeoutSeconds` (1 to 86400). `capture.files` names the JSON files the script
writes there; each is copied back to the evidence directory with mode 600.

Each check has a `file`, a `pointer` (an
[RFC 6901](https://www.rfc-editor.org/rfc/rfc6901) JSON pointer into that
file), a `message` to report when it fails, and exactly one test:

| Test | Passes when the value |
| --- | --- |
| `equals` | equals the given JSON value |
| `oneOf` | equals one of the given JSON values |
| `matches` | is a string in which the given regular expression is found |
| `items` | is a list with exactly `count` selected items, and each selected item is an object whose fields equal those in `each`; the selected items are the objects whose fields equal those in `where`, or every item without `where` |

Equality is by JSON type: `true` is not `1`, and `42` is not `42.0`. A
pointer that resolves to nothing fails its check. With `where`, a list that
holds anything but objects fails. Profiles and evidence files are read
strictly: a duplicate key, `NaN`, or `Infinity` is refused.

Failed checks report their messages in profile order, each distinct message
once. The clone check fails if any check fails, if the boot ID is not a UUID,
or if the boot IDs differ.

`receiptV1Runtime` is the `runtime` block that the profile's `@1` receipts
carry. Only a profile that verifies `@1` receipts has one (see
[Receipts](#receipts)).

### The appliance profile

`clone-profile.json` restates the checks that `verify-vagrant-clone.py` and
`verify-vagrant-clone-receipt.py` made in code before `#185`: the LabVIEW
known-answer probe, the guest OS, the LabVIEW package version, automatic login
off, the VIPM version, build, edition, and activation, one LabVIEW 2026 64-bit
target on VI Server port 3363, and the consumer's sample package at its
expected version. A build number of `"4056"` or `4056` passes, as before.

`scripts/tests/test_vagrant_clone.py` fails if the profile's OS, LabVIEW
package version, LabVIEW year, VI Server port, or VIPM version differs from the
manifest.

The appliance profile refuses runner units (`#192`). A clone with a runner
unit would start a copy of the source VM's runner, with its registration, so
every clone check of such a box fails before the capture runs. The appliance's
`register-runner` phase installs that unit on the source VM and takes no
snapshot, and `package-vagrant-box.py` packages the source VM's current state
with `vagrant package --base`: its current-snapshot check does not exclude
changes made after the snapshot. Package the box before registering a runner
on the source VM.

`clone-profile.v1.json` is the appliance profile as it was before `#192`: the
same checks, with `runnerUnits` set to `record`, plus `receiptV1Runtime`. It
is frozen. `verify-vagrant-clone-receipt.py` pins its SHA-256 and reads it
only for `@1` receipts (see [Receipts](#receipts)).

## Runner units

The check lists the guest's units with these two commands, over SSH:

- `systemctl list-unit-files --no-pager --no-legend --plain --full`
- `systemctl list-units --all --no-pager --no-legend --plain --full`

Neither takes a pattern: with a pattern that matches nothing,
`list-unit-files` exits 1 and prints nothing, which looks like a failure. A
unit whose name matches `actions\.runner\..+` is a runner unit. The check
fails closed: a non-zero exit, an empty listing, or a line that does not start
with a unit name stops the check.

With `runnerUnits` set to `refuse`, any runner unit stops the check before
the capture runs. With `record`, the receipt records the units it found.

## Checking an appliance clone (host)

Start the clone with Vagrant first, then plan the check:

```bash
vm/ubuntu-labview/verify-vagrant-clone.py \
  --vm-name <clone name> --vm-uuid <clone UUID> \
  --vagrant-machine-id-file <run directory>/.vagrant/machines/default/virtualbox/id \
  --ssh-port <port> --guest-password-file <mode-600 file> \
  --box <box> --manifest vm/ubuntu-labview/provisioning-manifest.json \
  --base-receipt <file> --public-receipt <file> --public-appliance-receipt <file> \
  --activation-receipt <file> --package-report <file> --package-artifact <file> \
  --embedded-vagrantfile <file> --box-receipt <file> --evidence-dir <mode-700 directory>
```

Without `--run`, it checks its inputs, the box receipt, and the clone, and
prints `PLAN` with the clone UUID, the SSH port, the box's SHA-256, and the
profile's name and SHA-256. It refuses when the profile's capture script is
missing, or when the evidence directory already holds the receipt, its hash,
or any evidence file the profile names. `--vboxmanage` names the `VBoxManage`
program when it is not on `PATH`. Add `--run` to run the check. With `--run`,
it:

1. Enrolls the expected host key, trying 30 times, 5 seconds apart.
2. Checks for runner units.
3. Copies the capture script to `/home/actor/projectbuddy-clone-proof` and
   runs it.
4. Copies back the evidence files and checks them against the profile.
5. Removes the guest directory and runs `systemctl poweroff`. If the clone is
   not powered off after 60 state checks, 2 seconds apart, it presses the ACPI
   power button and checks the same way again.
6. Writes `vagrant-clone-receipt.json`, verifies it, and writes
   `vagrant-clone-receipt.json.sha256`. Neither file replaces an existing one.

The SSH password reaches `ssh` and `scp` through an askpass script in a
private `projectbuddy-vagrant-clone-*` directory under `$XDG_RUNTIME_DIR`, or
the system temporary directory when that is unset, which is removed afterward.
If the check fails, it removes the receipt and hash it wrote, and it powers
the clone off whenever it can. The evidence files it copied back stay, so the
failure can be diagnosed; move them away before checking again.

## Receipts

The check writes a `projectbuddy/ubuntu-vagrant-clone@2` receipt:

| Field | Meaning |
| --- | --- |
| `profile` | the profile's `name` and `sha256` |
| `sourceBox` | the box receipt's SHA-256 (`receiptSha256`) and the box SHA-256 it records (`boxSha256`) |
| `clone` | the clone's `vm` `name` and `uuid`, its `bootId`, and its `finalState`, `poweroff` |
| `runnerUnits` | the profile's `policy` and the runner units `observed`, sorted |
| `evidenceSha256` | the SHA-256 of each evidence file, by file name |
| `failures` | `[]` |
| `outcome` | `PASS` |

To verify a receipt:

```bash
vm/ubuntu-labview/verify-vagrant-clone-receipt.py vagrant-clone-receipt.json \
  --box-receipt <file> --evidence-dir <directory> --vm-name <clone name> --vm-uuid <clone UUID>
```

The verifier rebuilds the whole receipt from the profile, the evidence, the
box receipt, and the clone's name and UUID, and fails on any difference. It
also re-runs the profile's checks on the evidence, refuses forbidden fields
such as `token` or `password` at any depth, and refuses a `refuse` profile's
receipt that records a runner unit. It cannot list the clone's units again, so
it checks `runnerUnits.observed` only for form (sorted, distinct runner unit
names) and against the policy.

It still verifies `projectbuddy/ubuntu-vagrant-clone@1` receipts, which carry
a `runtime` block and name no profile. An `@1` receipt verifies only against
the frozen `clone-profile.v1.json`, whose SHA-256
`verify-vagrant-clone-receipt.py` pins (`V1_PROFILE_SHA256`), and its
`runtime` must equal that profile's `receiptV1Runtime`. `--profile` does not
apply to `@1` receipts, and a changed `clone-profile.v1.json` stops them from
verifying. The verifier reads both profiles for every receipt, so an
unreadable one fails any receipt.

An `@2` receipt names the profile it was checked with. One written under the
appliance profile from before `#192` verifies with
`--profile vm/ubuntu-labview/clone-profile.v1.json`.

The new verifier is stricter than the code it replaces: a receipt with a
field the receipt does not define, a duplicate key, or a value of the wrong
JSON type fails.

## Exit codes

| Code | `verify-vagrant-clone.py` | `verify-vagrant-clone-receipt.py` |
| --- | --- | --- |
| `0` | `PLAN` printed, or the check passed | the receipt verified |
| `1` | the check failed during `--run`; the clone is powered off when it can be | |
| `2` | an input, the box receipt, or the clone was refused before anything changed | the receipt did not verify |
| `127` | `VBoxManage` is missing | |

## Related

- [The VM provisioning procedure](procedure.md#receipt-verification-chain)
- [The Ubuntu lane box procedure](ubuntu-lane-box.md)
- [The Ubuntu lane procedure](ubuntu-lane.md)
