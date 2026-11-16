# `probes/`: `projectbuddy-probes`

`projectbuddy-probes` is the closed list of **read-only** tools that the stage
P4 diagnostics agent may call on the peer
([#292](https://github.com/VITechnologies/projectbuddy/issues/292), slice 2 of
[#290](https://github.com/VITechnologies/projectbuddy/issues/290) in
[#210](https://github.com/VITechnologies/projectbuddy/issues/210)). Each tool
answers one fixed question about the machine. Every answer is:
- bounded;
- deterministic for a given machine state;
- redacted by [`../redact`](../redact/README.md) before it is returned;
- capped in bytes.

It is a self-contained .NET 10 single-file program, like `projectbuddy-advise`,
and it uses the shared guard library in [`../ollama`](../ollama/README.md).

```bash
dotnet publish ProjectBuddy.PeerProbes -c Release -r win-x64 -o out
out/projectbuddy-probes.exe list
out/projectbuddy-probes.exe run event-errors --args '{"hours":6}' --deny-list <private deny list>
out/projectbuddy-probes.exe run-all --deny-list <private deny list>
```

## Tools

| Tool | Arguments | Reads | Reports |
|---|---|---|---|
| `network-link` | none | .NET `NetworkInterface` | Per adapter (loopback left out): name, kind (`ethernet`, `wifi`, `tunnel`, `other`), status, speed in Mbit/s, IPv4 and IPv6 binding, default gateways. No MAC addresses. |
| `disk` | none | .NET `DriveInfo`, fixed and ready drives | Name, file system, total and free bytes. Read-only images (`squashfs` and similar) and kernel pseudo file systems are left out. |
| `ollama` | none | Ollama on loopback: `/api/version`, `/api/tags`, `/api/ps` | Version; installed models by digest and name, with `identity: true` on the pinned `projectbuddy` model; loaded models with size, VRAM and context length. `{"reachable": false}` when Ollama does not answer. |
| `vm-states` | none | `VBoxManage list vms`, `list runningvms`, `showvminfo --machinereadable` | Per VM: name, UUID, running (`null` when the running list failed), state and its change time, OS type, memory, CPUs, VRDE on or off, current snapshot name and UUID. |
| `services` | none | `sc.exe query` and `sc.exe qc`, `schtasks.exe /query /fo csv /v` | The `gitlab-runner`, `actions.runner.*` and `ProjectBuddy-GitLabRunner-*` services (state, start type), and the `ProjectBuddy-Bus` and `ProjectBuddy-Ollama` tasks (status, last result, or `absent`). Windows only. |
| `event-errors` | `hours`, 1 to 72, default 24 | `wevtutil.exe qe System` and `Application`, level 1 and 2, newest 2000 per log | Counts grouped by log, provider, event ID and level. No message text and no event data. Windows only. |

`list` prints these specs as `projectbuddy/peer-tools@1` JSON, the input for
the agent loop's tool definitions.

### What a tool never reads

- `showvminfo` output is read **only** for the eight keys above, by exact name.
  `vrdeproperty[...]` (for example a VNC password) and every other key are
  skipped unread.
- Services are queried only when their name is on the allow-list. Scheduled
  tasks are queried by their exact names.
- From each event, only `Provider@Name`, `EventID` and `Level` directly under
  the event's own `<System>` header are read. Event data, user data and
  rendered messages are never touched.
- Programs are run without a shell, with fixed argument lists, and never from
  `PATH`:
  - `sc.exe`, `schtasks.exe` and `wevtutil.exe` come from the Windows system
    directory;
  - `VBoxManage` comes from `--vboxmanage` when it is given, and otherwise from
    VirtualBox's default install directory.
- Each program gets 60 seconds and its output is cut at 16 MiB. Either limit
  marks the result truncated.

## Arguments and refusals

A call names one tool from the list. Its arguments are a JSON object holding
only that tool's arguments: whole numbers inside their range. An unknown tool,
an unknown or extra argument, a value that is not a whole number, or a value
out of range is **refused** (exit 3), and nothing is run.

`run-all` checks its `--hours` before any tool runs.

## Result envelope (`projectbuddy/peer-tool-result@1`)

```json
{"schema":"projectbuddy/peer-tool-result@1","tool":"disk","truncated":false,"redaction":{"private":1},"data":{"drives":[...]}}
```

- **Redaction.** `data` is redacted as JSON text with
  `Redactor.ForHost(<deny list>, <machine name>, <user name>)`, and the
  redacted text must parse again. `redaction` holds the counts per rule.
  - Numbers, booleans and `null` after a key stay as they are.
  - The host's own machine and account names are always redacted.
  - A deny list that cannot be read or is invalid refuses the call; it never
    redacts with fewer terms.
- **Caps.** Each list has a fixed item cap:

  | List | Cap |
  |---|---|
  | adapters | 16 |
  | drives | 26 |
  | models | 64 |
  | loaded models | 64 |
  | VMs | 32 |
  | services | 16 |
  | event groups | 50 |

  Going over a cap sets `truncated: true`. So does a cut command output or
  2000 events in one log.
- **Byte cap.** The whole envelope is at most 16 KiB. Over that, items are
  dropped from the end of the tool's lists, in order, and `truncated` is set.
- **Failures.** A result is replaced by a failure envelope with an `error` and
  `truncated: true` when:
  - the result still does not fit once the lists are empty;
  - the redacted text no longer parses;
  - the read itself throws. Only the exception's type is reported, because its
    message can carry paths.

  A failure envelope is smaller than the 256-byte minimum cap.
- Lists are sorted with ordinal comparison, so the same machine state gives the
  same bytes.

## Commands and exit codes

```text
projectbuddy-probes list
projectbuddy-probes run <tool> [--args <json object>]
projectbuddy-probes run-all [--hours <1-72>]
  [--deny-list <path>] [--ollama http://127.0.0.1:11434/] [--vboxmanage <path>] [--identity <model>]
```

- `run` writes one envelope.
- `run-all` writes one envelope per tool, in list order, then a
  `projectbuddy/peer-tool-run@1` line with the SHA-256 of each envelope line.
  A live proof records only those hashes and the redacted envelopes.
- `--ollama` must be `http://` followed by a loopback IP literal and a port. The
  client ignores proxy settings and refuses redirects.
- `--identity` names the pinned model. The default is
  `projectbuddy:0.1-qwen2.5-7b`.

| Exit code | Meaning |
|---|---|
| 0 | Results written |
| 2 | Usage error |
| 3 | Refused: unknown tool, bad arguments, an unreadable or invalid deny list, or a non-loopback `--ollama` |

## Assumptions to confirm on the peer

The parsers are tested on synthetic samples. The first live run on the peer
(gated by its own approval) confirms the following:
- `sc.exe` and `schtasks.exe` print English field names. `schtasks` columns
  are found by header name.
- `wevtutil` output decodes as UTF-8.
- `NetworkInterface.Supports(IPv6)` reflects IPv6 being unbound from an
  adapter.

## Tests

```bash
dotnet test ProjectBuddy.PeerProbes.Tests --settings coverage.runsettings --collect:"XPlat Code Coverage"
```

- The tests run the registry against a scripted `ISources`: every tool shape,
  every refusal, every cap, truncation, the failure envelopes, and real
  redaction of every tool.
- The parsers are tested on synthetic samples.
- The command line is tested with injected writers.
- CI adds it to the `Peer Tool Tests` matrix at 100% line and branch coverage
  and publishes the trimmed win-x64 binary.
- The operating-system adapter (`SystemSources`) is excluded from coverage. It
  makes the reads and takes no decisions.
