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
powershell -NoProfile -ExecutionPolicy Bypass -File .\Get-PeerLinkDiagnostics.ps1 -OutputDirectory <folder>
```

## `ask/`: `projectbuddy-ask`

A self-contained, single-file C# (.NET 10) tool with two modes.

- `projectbuddy-ask serve` is the peer's SSH forced command.
  - It reads `SSH_ORIGINAL_COMMAND` and accepts exactly one of `identity`,
    `vbox`, `disk`, `ollama` or `lbabus`.
  - It answers with `projectbuddy/peer-ask@1` JSON of at most 16 KiB.
  - It refuses anything else with exit code 64 and a fixed message.
  - `vbox` reports only VMs named `projectbuddy-*`; other VMs are only counted.
- `projectbuddy-ask ask <verb> --host <address> --user <name> --identity <key> --known-hosts <file> [--connect-timeout <seconds>]`
  is the workstation client.
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
    | 8 | peer error |
    | 9 | protocol |

The key on the peer carries `from="<workstation address>",restrict,command="<path>\projectbuddy-ask.exe serve"`.
The peer's `sshd_config` is key-only and denies every other source address.
A firewall rule limits port 22 to the cable adapter and the workstation
address.

Build and test it (`ci-assurance.yml` requires 100% line and branch coverage):

```bash
cd tools/peer/ask
dotnet test ProjectBuddy.PeerAsk.Tests --settings coverage.runsettings --collect:"XPlat Code Coverage"
dotnet publish ProjectBuddy.PeerAsk -c Release -r win-x64 -o out   # the peer's projectbuddy-ask.exe
```
