# Peer link tools

Tools for the peer-link epic
([#210](https://github.com/VITechnologies/projectbuddy/issues/210)). Stage P0
([#211](https://github.com/VITechnologies/projectbuddy/issues/211)) connects a
workstation to a second machine (the peer) over a direct Ethernet cable. The
workstation can then ask the peer a fixed set of read-only questions over a
key-only SSH channel.

No operator values (addresses, account names, keys or host names) belong in
this directory. They live in the operator's private notes and private
consumer repository.

## `Get-PeerLinkDiagnostics.ps1`

A read-only Windows PowerShell report for planning the link. It changes nothing
on the machine and writes one text file. The report covers:

- network adapters, IP addresses and routes, including VPN adapters;
- firewall profiles, and rules that touch SSH or TCP port 22;
- the OpenSSH Server state;
- sleep and network-adapter power settings, and recent link events;
- VirtualBox VM counts per local profile (no VM names);
- Ollama, the .NET SDKs and runtimes, and free disk space.

The report contains private network details, so keep it out of public
repositories. To run it:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\Get-PeerLinkDiagnostics.ps1 -OutputDirectory <folder> [-CopyToClipboard]
```

## `ask/`: `projectbuddy-ask`

A self-contained, single-file C# (.NET 10) tool with two modes.

- `projectbuddy-ask serve` is the peer's SSH forced command.
  - It reads `SSH_ORIGINAL_COMMAND` and accepts exactly one of `identity`,
    `vbox`, `disk`, `ollama`, `lbabus`, `diagnose <question-id>` (see
    [`diagnose/`](diagnose/README.md)) or `infer` (below).
  - It answers with `projectbuddy/peer-ask@1` JSON of at most 16 KiB, or
    1 MiB for `infer`.
  - It refuses anything else with exit code 64 and a fixed message.
  - `vbox` reports only VMs named `projectbuddy-*`; other VMs are only counted.
- `projectbuddy-ask ask <verb> --host <address> --user <name> --identity <key> --known-hosts <file> [--connect-timeout <seconds>]`
  is the workstation client. `ask diagnose <question-id>` and
  `ask infer --request <file>` take the same options.
  - It runs one fixed `ssh` invocation: `-F none`, the given identity only, the
    pinned known-hosts file, strict host-key checking, `BatchMode`, a connect
    timeout and no forwarding.
  - It validates the answer.
  - It reports a failure by exit code:

    | Exit code | Meaning |
    | --- | --- |
    | 2 | usage |
    | 3 | refused |
    | 4 | host key |
    | 5 | authentication |
    | 6 | unreachable |
    | 7 | ssh error |
    | 8 | peer error (including the peer's output limit, exit 65, and internal errors, exit 66) |
    | 9 | protocol |

The key on the peer carries `from="<workstation address>",restrict,command="<path>\projectbuddy-ask.exe serve"`.
Windows OpenSSH runs the forced command through `cmd.exe`, so use a path
without spaces. Keep the file in a directory such as
`C:\ProgramData\ProjectBuddy\peer-ask` that only Administrators and SYSTEM can
write to.

The tools that `serve` runs (VBoxManage, Ollama and lbabus) are started by
absolute path, never by bare name. The client requires absolute paths for
`--identity`, `--known-hosts` and `--ssh`. `--ssh` defaults to the system
OpenSSH client.
The peer's `sshd_config` is key-only and denies every other source address.
A firewall rule limits port 22 to the cable adapter and the workstation
address.

Build and test it (`ci-assurance.yml` requires 100% line and branch coverage).
`global.json` pins the .NET SDK exactly, because the NuGet lock files record
the SDK's bundled trimming package. After a deliberate SDK bump, regenerate the
lock files with `dotnet restore --force-evaluate`.

```bash
cd tools/peer/ask
dotnet test ProjectBuddy.PeerAsk.Tests --settings coverage.runsettings --collect:"XPlat Code Coverage"
dotnet publish ProjectBuddy.PeerAsk -c Release -r win-x64 -o out   # the peer's projectbuddy-ask.exe
```

### `infer`: batched local inference (#358)

The workstation sends a batch of prompts to the peer's GPU.

- **Request:** ssh's stdin, at most 1 MiB of UTF-8 JSON with no byte-order
  mark:
  `{"schema":"projectbuddy/peer-infer-request@1","model":"<name:tag>","options":{"temperature":0,"seed":42,"num_ctx":4096},"items":[{"id":"<id>","prompt":"<text>","format":"json"}]}`
  - 1 to 64 items. Ids are unique, 1 to 64 of `[A-Za-z0-9._:-]`. A prompt
    is 1 byte to 64 KiB. `format` is `"json"`, `null` or absent.
  - `options` and each option are optional: `temperature` 0 to 2, `seed`
    0 to 2147483647, `num_ctx` 512 to 32768.
  - Any other key, a duplicate key, or a value out of range is refused (64).
- **Allowlist:** the model must be listed with its digest in
  `%ProgramData%\ProjectBuddy\peer-ask\infer\allowlist.json`
  (`~/.local/share/projectbuddy/peer-ask/infer/allowlist.json` on Linux),
  which deployment writes:
  `{"schema":"projectbuddy/peer-infer-allowlist@2","models":[{"name":"<name:tag>","digest":"<64 hex>","ports":[11434,11435]}]}`.
  - At most 64 KiB and 64 models; names are lowercase `name:tag`.
  - `ports` picks the peer's loopback Ollama servers: 11434 (NVIDIA GPU)
    and 11435 (Intel Arc iGPU, Vulkan). The set is fixed. The list is
    non-empty and distinct, and defaults to `[11434]`. An `@1` file has no
    `ports` and means `[11434]`.
  - Each listed server's `/api/tags` must show the model with exactly that
    digest. The peer never pulls. A missing or malformed allowlist refuses
    everything.
- **Run:** each item is one non-streaming `/api/chat` call with one user
  message and no system prompt, tools or images. It goes to Ollama at
  `127.0.0.1` through the loopback-only client in
  [`ollama/`](ollama/README.md). Each listed port runs four workers that take
  items from one shared queue, so both servers stay busy. Each item has
  120 s; the whole call, including reading the request, has 600 s.
- **Answer:** `data` is
  `{"model","digest","items":[{"id","answer","evalCount","promptEvalCount","seconds","port"}]}`,
  in request order; `port` is the server that answered. The caller can check
  `promptEvalCount < num_ctx/2` (truncation guard).
- **Failure:** a timeout, an Ollama fault or any failed item fails the whole
  call (66), with no partial answer. An answer over 1 MiB is 65.
- **Client:** sends the file's bytes unchanged, after the same checks. It
  waits 620 s plus the connect timeout. It accepts only an answer that names
  the model and a digest and answers every requested id exactly once, from
  an allowed port.
- **Security:** the request carries no path, command or tool, and nothing
  runs through a shell. Only the model's output for the submitted prompts
  and its counters leave the peer. Allowlist only models whose Modelfile
  holds nothing confidential.

## `identity/`: the `projectbuddy` model identity

`identity/identity.json` pins the identity for stage P1
([#217](https://github.com/VITechnologies/projectbuddy/issues/217)). It holds a
fixed role text, the Ollama parameters (`num_ctx`, `temperature`, `seed`), a
size limit for the system prompt, and every source file with its SHA-256.
`scripts/New-PeerModelfile.py` turns the manifest and a base model into a
Modelfile. The same inputs always give byte-identical output, and a changed
source fails closed:

```bash
python3 scripts/New-PeerModelfile.py --manifest tools/peer/identity/identity.json --base qwen2.5:7b --output Modelfile
ollama create projectbuddy:0.2-qwen2.5-7b -f Modelfile
```

Editing a pinned source (this README is one of them) means updating its digest
in `identity.json`, and raising `identityVersion` when the change affects the
model.
