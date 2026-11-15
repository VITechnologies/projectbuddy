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

## Input

- Evidence over `Redactor.MaxInputChars` (4,000,000) is **refused whole**
  (`RedactionException`), never partly redacted.
- Text is normalized to NFC first. Invisible format characters (zero-width
  spaces and joiners, soft hyphens) are removed and lone surrogates become
  `U+FFFD`, so nothing a reader cannot see can split a term or an address.

## Rules, in order

| Rule | Becomes | Kept |
|---|---|---|
| Private keys; known token formats (GitHub, GitLab, cloud, chat, JWT, npm, Google API) | `<secret>` | — |
| `Authorization`/`Proxy-Authorization` values, to the closing quote or the end of the line | `<secret>` | the header name and a well-known scheme (`Bearer`, `Basic`, `token`, `AWS4-HMAC-SHA256`, …) |
| Token-like `Bearer`/`Basic` credentials elsewhere (16+ characters with a digit) | `<secret>` | the scheme; prose such as "Basic authentication" stays |
| XML `key="…Password…" value="…"`, flags (`--token x`, `-password=x`), assignments whose key contains a secret word, with any prefix (`DB_PASSWORD`, `ACTIONS_RUNNER_INPUT_TOKEN`), separated by `:`/`=` or a table gap of 2+ spaces; in `;`-delimited strings the value runs to the `;` | `<secret>` | the key and separator |
| SAS signatures (`?…&sig=…`) | `sig=<secret>` | the other query fields |
| URLs | `<url>` | `scheme://host[:port]` for allow-listed hosts, with the path **always** replaced by `/<path>`; user info is dropped |
| `[user@]host:path` git remotes (user-less only when the path ends in `.git`), e-mail addresses | `<url>`, `<email>` | — |
| Drive paths in every spelling: `X:\`, `X:/`, JSON-escaped `X:\\`, `file:///X:/`, `X%3A/`, `\\?\X:\`, `\\.\X:\` | `<path>` | allow-listed prefixes to a fixed depth (below) |
| UNC and device paths (`\\server\share`, `\\?\UNC\…`, `\\.\pipe\…`), Git Bash and WSL drive paths (`/c/…`, `/mnt/c/…`) | `<path>` | — |
| User profiles (`C:\Users\x`, `/home/x`, `/Users/x`, any case) | `<user>`, and the rest `<path>` | allow-listed profile folders to a fixed depth (below) |
| MAC addresses; IPv4/IPv6 outside the allowed networks, including one that ends a sentence | `<mac>`, `<ip>` | loopback, the unspecified address and the cable `10.211.0.0/30`; versions such as `Version=13.0.0.0` and `v1.2.3.4` |
| Bare host names with a known top-level domain (`fs01.corp.example.com`) | `<host>` | allow-listed hosts, again with any path replaced by `/<path>` |
| Deny-list terms (case-insensitive, longest first, words joined by any whitespace) | `<private>` | placeholders are never rewritten |

**What is deliberately kept,** because diagnosis needs it: hex digests and hashes,
version and build numbers, exit codes, times, file names such as `main.vi` or
`ollama.exe`, .NET namespaces, and PowerShell `::` calls.

## Paths

A path match runs on through spaces and through apostrophes inside names
(`O'Brien`), so `D:\Acme Projects\x` never leaks its tail. It stops:
- at a colon, which no Windows path component can hold (`file.cs:line 42`,
  `proj.csproj(12,5): error`);
- before the next drive or UNC path, so a kept path never carries another one
  along.

Segments are split on **every** separator spelling, so mixing them
(`C:\Windows\Temp/x`) cannot dodge a limit. The kept part is copied from the
original text, so `cmd.exe /c` keeps its switch as written. Any `..` segment
turns the whole path into `<path>`. A bare drive root (`C:\`) names nothing and
stays.

An `AllowedPath(prefix, depth)` keeps the prefix plus `depth` more segments; the
rest becomes `<path>`. The longest matching prefix wins:

| Prefix | Depth |
|---|---|
| `C:\Windows` | 4 |
| `C:\Windows\Temp`, `C:\Windows\ServiceProfiles`, `C:\Windows\System32\config`, `C:\Windows\SysWOW64\config` | 0 |
| `C:\Program Files`, `C:\Program Files (x86)` | 3 |
| `C:\ProgramData\ProjectBuddy` | 2 |
| Profile: `AppData\Local\Programs`, `AppData\Local\ProjectBuddy`, `.ollama` | 2 |

Prefixes must start with a drive root, and depths must be zero or more.

## Performance

The URL, scp-remote, e-mail and deny-list patterns run on .NET's
non-backtracking engine. Every other unbounded scan is either linear or capped
(a `;`-delimited value is at most 256 characters). The tests redact a megabyte
of each adversarial input shape (repeated `a.`, `a\`, `C:\`, `password=a `, …)
and require it to finish well inside interactive time.

## The deny list is operator-private

Customer, project, machine and account names are **never** committed. The
operator keeps them in a file on the peer (`projectbuddy/redaction-denylist@1`:
`{"schema": …, "terms": [...]}`).
- `Redactor.ForHost` always adds the machine and account names, even ones such
  as `User` that overlap a placeholder.
- Each term is 3–200 characters on one line, with at most 1000 terms.
- Terms match as substrings, so a term inside a longer word is redacted too.

## Corpus gate

`corpus.json` (`projectbuddy/redaction-corpus@1`) is **synthetic**. Each item
lists strings that must not survive and strings that must stay, and every item
must also be idempotent. Token-shaped values are written as `{{templates}}` and
assembled at test time, so no token-like literal is ever committed.

Run it with `dotnet test ProjectBuddy.PeerRedact.Tests`. CI requires 100% line
and branch coverage.
