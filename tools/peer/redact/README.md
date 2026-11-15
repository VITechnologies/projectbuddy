# `redact/`: deterministic redaction for peer evidence

`ProjectBuddy.PeerRedact` removes identifying and secret data from peer evidence:
- **before** a local model sees it;
- **before** anything leaves the peer.

This covers #210 D5 and D6 for the P4 diagnostics agent
([#290](https://github.com/VITechnologies/projectbuddy/issues/290)). The output
is deterministic and **idempotent**: redacting redacted text changes nothing.

```csharp
var terms = Redactor.ParseDenyList(File.ReadAllBytes(privateDenyListPath));
var redactor = Redactor.ForHost(terms, Environment.MachineName, Environment.UserName);
Redaction result = redactor.Redact(evidence);   // result.Text, result.Counts per rule
```

| Rule | Becomes | Kept |
|---|---|---|
| Private keys, known token formats (GitHub, GitLab, cloud, chat, JWT, npm, Google API), `Bearer`/`Basic` credentials, `password=`/`token:`/`api_key`… values (including JSON) | `<secret>` | the key name |
| URLs | `<url>` | `scheme://host[:port]` for allow-listed hosts, with the path **always** replaced by `/<path>`; user info is dropped |
| `user@host:path` git remotes, e-mail addresses | `<url>`, `<email>` | — |
| UNC and drive paths | `<path>` | allow-listed system prefixes, verbatim. A path match runs on through spaces but stops before the next drive or UNC path, so a kept path never carries another one along. |
| User profiles (`C:\Users\x`, `/home/x`, `/Users/x`) | `<user>`, and the rest `<path>` | allow-listed profile folders (`AppData\Local\Programs\`, `AppData\Local\ProjectBuddy\`, `.ollama\`) |
| MAC addresses, IPv4/IPv6 outside the allowed networks | `<mac>`, `<ip>` | loopback, the unspecified address and the cable `10.211.0.0/30` |
| Deny-list terms (case-insensitive, longest first) | `<private>` | — |

**What is deliberately kept,** because diagnosis needs it: hex digests and hashes,
version and build numbers, exit codes, times, and PowerShell `::` calls.

## The deny list is operator-private

Customer, project, machine and account names are **never** committed. The
operator keeps them in a file on the peer (`projectbuddy/redaction-denylist@1`:
`{"schema": …, "terms": [...]}`).
- `Redactor.ForHost` always adds the machine and account names.
- Each term is 3–200 characters on one line, with at most 1000 terms.
- A term may not occur inside a placeholder, which keeps the output idempotent.

## Corpus gate

`corpus.json` (`projectbuddy/redaction-corpus@1`) is **synthetic**. Each item
lists strings that must not survive and strings that must stay, and every item
must also be idempotent. Token-shaped values are written as `{{templates}}` and
assembled at test time, so no token-like literal is ever committed.

Run it with `dotnet test ProjectBuddy.PeerRedact.Tests`. CI requires 100% line
and branch coverage.
