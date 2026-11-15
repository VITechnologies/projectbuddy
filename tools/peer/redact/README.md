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

## Input and passes

- Evidence over `Redactor.MaxInputChars` (4,000,000) is **refused whole**
  (`RedactionException`), never partly redacted.
- JSON `\uXXXX` escapes (the default .NET `Utf8JsonWriter` and Windows
  PowerShell 5.1 `ConvertTo-Json` escape `'`, `"`, `<`, `>`, `&` and every
  non-ASCII character) become their shortest JSON spelling. Printable
  characters are written literally, and a quote and a backslash as `\"` and
  `\\`. JSON meaning is unchanged, but `\u0027D:/Clients` or `M\u00FCller` can
  no longer hide a path or a term. Escapes of invisible characters left at a
  deeper escape level (JSON inside JSON) are removed.
- Text is then normalized to NFC. Invisible format characters are removed,
  including those outside the Basic Multilingual Plane (zero-width spaces and
  joiners, soft hyphens, tag characters), and lone surrogates become `U+FFFD`.
  Nothing a reader cannot see can split a term or an address.
- The rules below run in order, and the whole order **repeats until the text
  stops changing**. A later pass is needed only when one rule's placeholder
  unlocks an earlier rule, so `Redactor.MaxPasses` (32) exceeds the number of
  rules: even a deliberate chain through every rule settles. Text that has not
  settled by then is refused. Idempotence therefore holds by construction. A
  seeded fuzz of 300,000 glued-together fragments never needed more than 4
  passes.

## Rules, in order

| Rule | Becomes | Kept |
|---|---|---|
| Private keys; known token formats (GitHub, GitLab, cloud, chat, JWT, npm, Google API) | `<secret>` | — |
| `Authorization`/`Proxy-Authorization` values, plain or in escaped JSON (`\"Authorization\":\"…\"`), after `:`, `=` or a table gap of 2+ spaces, to the closing quote or the end of the line | `<secret>` | the header name and a well-known scheme (`Bearer`, `Basic`, `token`, `AWS4-HMAC-SHA256`, …) |
| Token-like `Bearer`/`Basic` credentials elsewhere (16+ characters with a digit) | `<secret>` | the scheme; prose such as "Basic authentication" stays |
| XML `key="…Password…" value="…"` | `<secret>` | the key |
| Command lines: the password argument of `net use` (first plain argument) and `net user` (second, after the account, which becomes `<user>`); `schtasks /P`, `/RP`; `sqlcmd`, `bcp`, `osql` `-P` (also glued, `-PSecret`); `psexec`, `certutil`, `docker login` `-p`; `7z -pSecret`. The matching account switches (`/U`, `/RU`, `-U`, `-u`) become `<user>`. Short flags count only after their own command, because a general `-p` is a port. | `<secret>` | the command and its switches |
| PowerShell: `ConvertTo-SecureString "…"`, `-String …` and `"…" \| ConvertTo-SecureString`; `NetworkCredential("user", "…")` (the user becomes `<user>`) | `<secret>` | the call |
| Flags: `--token x`, `-pw x`, `--password=x`, `-passin`/`-passout pass:x`, `/pass:x`, `/password:x` | `<secret>` | the flag |
| Assignments whose key contains a secret word, with any prefix (`DB_PASSWORD`, `ACTIONS_RUNNER_INPUT_TOKEN`, `"api_key"`, `\"password\"`), or whose key is `pw` or `pass` as a whole word part (`$pw`, `db_pass`, but not `bypass`), after `:`/`=` or a table gap. A quoted value (plain or escaped) is taken whole. After a `;` (a connection-string item), the value runs to the next `;`, the line end or a quote; elsewhere a `;` ends it. Both are capped at 256 characters. A value that starts with a credential cmdlet (`$pw = Read-Host …`) is code and stays. | `<secret>` | the key and separator |
| SAS signatures (`?…&sig=…`) | `sig=<secret>` | the other query fields |
| Accounts: `/user:x`, `/u:x`, `--user=x`, `--username x`, `User Id=x`, `UserName=x` | `<user>` | the switch or key |
| URLs, plain or JSON-escaped (`https:\/\/`) | `<url>` | `scheme://host[:port]` for allow-listed hosts, with the path **always** replaced by `/<path>`; user info is dropped |
| `[user@]host:path` git remotes (user-less only when the path ends in `.git`), e-mail addresses | `<url>`, `<email>` | image digests (`alpine@sha256:…`) |
| Drive paths in every spelling: `X:\`, `X:/`, JSON-escaped `X:\\`, `file:///X:/`, `X%3A%2F`, `\\?\X:\`, `\\.\X:\` | `<path>` | allow-listed prefixes to a fixed depth (below) |
| UNC and device paths (`\\server\share`, `\\?\UNC\…`, `\\.\pipe\…`, `//server/share`, `file:////server/share`); Git Bash, WSL and Cygwin drive paths (`/c/…`, `/mnt/c/…`, `/cygdrive/c/…`) | `<path>` | — |
| User profiles (`C:\Users\x`, `/home/x`, `/Users/x`, any case) | `<user>`, and the rest `<path>` | allow-listed profile folders to a fixed depth (below) |
| MAC addresses; IPv4/IPv6 outside the allowed networks, including one that ends a sentence | `<mac>`, `<ip>` | loopback, the unspecified address and the cable `10.211.0.0/30`; versions such as `Version=13.0.0.0`, `"ProductVersion": "1.0.0.0"` and `v1.2.3.4` |
| Directory names and certificate subjects: `CN=`, `OU=`, `DC=` values, and `O=`, `L=`, `S=`/`ST=`, `E=` inside a name (after `,`, `/` or `Subject:`/`Issuer:`) | `<private>` | allow-listed hosts (`CN=github.com`) and public certificate authorities and vendors (`DigiCert`, `GlobalSign`, `Let's Encrypt`, `Microsoft`, …) |
| Bare host names (below) | `<host>` | allow-listed hosts, again with any path replaced by `/<path>` |
| The account in `user@<ip>` or `user@<host>` (`ssh jane@10.0.0.5`) | `<user>` | — |
| Deny-list terms (case-insensitive, longest first, words joined by any whitespace) | `<private>` | placeholders are never rewritten |

**What is deliberately kept,** because diagnosis needs it: hex digests and hashes,
version and build numbers, exit codes, times, file names such as `main.vi` or
`ollama.exe`, .NET namespaces (`System.IO`, `Microsoft.NETCore.App`, `ASP.NET`),
Windows `.com` programs under `System32` and `SysWOW64` (`chcp.com`), and
PowerShell `::` calls.

A rule never takes a value that is already a placeholder again, so a settled
`{"password":<secret>}` keeps its closing brace.

## Paths

A drive starts after a non-alphanumeric character, a JSON `\t`, `\n` or `\r`
escape, or a short switch (`-oD:\x`, `/ID:\x`). Never inside a word, so
`https://` is not a drive `s:`.

A path match runs on through spaces and through apostrophes inside names
(`O'Brien`), so `D:\Acme Projects\x` never leaks its tail. It stops:
- at a quote, `<`, `>`, `|`, a tab, a line end or a wildcard;
- at a colon, which no Windows name can hold (`file.cs:line 42`,
  `proj.csproj(12,5): error`). A colon before a backslash is kept only when its
  letter cannot start a drive (`abcD:\x`), so a tail never leaks;
- before a switch (` /c`): no Windows name starts with `/` or ends with a space;
- before the next drive or UNC path, so a kept path never carries another one
  along.

So a path that is not allowed takes the rest of its line up to one of these
stops. That is fail-closed, at the cost of table columns that follow a path on
the same line. Switch arguments after a path stay visible
(`D:\Work\run.bat /project X`), so only the deny list protects their values.
POSIX paths have no switch or drive stop, since a POSIX name can hold anything.
An escaped quote (`\"`) also ends a path, so escaped JSON stays well formed.

In double-escaped JSON, `\\u0027` after a space looks exactly like a UNC path
(`\\server`) and is redacted as one. That is over-redaction, never a leak.

Segments are split on **every** separator spelling, and a run of separators
counts as one, so mixing or doubling them (`C:\Windows\Temp/x`,
`C:/Users//Jane`) cannot dodge a limit. The kept part is copied from the
original text. Prefixes match the way Windows resolves names, ignoring trailing
dots and spaces (`Temp.` is `Temp`). Any `.` or `..` segment, or a blank name
before a real one (`C:\Users\ \Jane`), turns the whole path into `<path>`. A
bare drive root (`C:\`) and trailing separators or spaces name nothing and stay.

An `AllowedPath(prefix, depth)` keeps the prefix plus `depth` more segments; the
rest becomes `<path>`. The longest matching prefix wins:

| Prefix | Depth |
|---|---|
| `C:\Windows` | 4 |
| `C:\Windows\Temp`, `SystemTemp`, `ServiceProfiles`, `Prefetch`, `Tasks`, `CSC`; `C:\Windows\System32\config`, `Tasks`, `spool`, `Microsoft\Protect`; `C:\Windows\SysWOW64\config` | 0 |
| `C:\Program Files`, `C:\Program Files (x86)` | 3 |
| `C:\ProgramData\ProjectBuddy` | 2 |
| Profile: `AppData\Local\Programs`, `AppData\Local\ProjectBuddy`, `.ollama` | 2 |

Prefixes must start with a drive root, and depths must be zero or more. Depth 3
under `Program Files` keeps a vendor's folder and program name, so a customer's
product installed there is caught only by the deny list.

## Host names

A bare name counts as a host when it ends in a known top-level domain:
- common generic domains (`com`, `net`, `org`, `edu`, `gov`, `mil`, `io`, `ai`,
  `co`, …) and internal ones (`local`, `lan`, `corp`, `internal`, `home`, …);
- country domains, so `contoso.com.au`, `contoso.co.jp` and `contoso.fr` count;
- labels may start with `_` (`_ldap._tcp.dc._msdcs.contoso.com`).

Two-letter country domains that are also common file extensions or identifiers
(`md`, `py`, `sh`, `so`, `id`, `at`, `it`, `in`, `no`, `pl`, …) count only after
two or more labels (`www.contoso.it`, but not `user.id` or `README.md`), or
after a DNS context word (`Suffix`, `Domain`, `ComputerName`,
`Resolve-DnsName`, `nslookup`, `ping`, `tracert`, `Test-NetConnection`,
`Test-Connection`). `vi` never counts, so LabVIEW files (`main.vi`) stay.

## Performance

The URL, scp-remote, e-mail, XML-setting and operator deny-list patterns run on
.NET's non-backtracking engine. Every lookbehind is bounded (whitespace inside
the version guard and the connection-string context is capped). Every other
unbounded scan was measured linear, or is capped (a `;`-delimited value is at
most 256 characters). The tests redact input at the 4,000,000-character cap for
each adversarial shape and require it to finish well inside interactive time.
The shapes include repeated `a.`, `C:\` and `password=a `; `key="` followed by
`pwd`; whitespace runs before digits; `/O=a`; `sqlcmd -P a`; and backslash runs
before `u0027`.

## The deny list is operator-private

Customer, project, machine and account names are **never** committed. The
operator keeps them in a file on the peer (`projectbuddy/redaction-denylist@1`:
`{"schema": …, "terms": [...]}`).
- Operator terms match as substrings, so a term inside a longer word is
  redacted too. Any character other than an ASCII letter or digit also matches
  its JSON escape at any depth (`O\\u0027Brien`, `M\\u00FCller`).
- `Redactor.ForHost` adds the machine and account names as **whole-word**
  terms: they match only between letters and digits, so an account `lab` leaves
  `LabVIEW` alone but still catches `lab-01` and `lab_build`. Names that overlap
  a placeholder, such as `User`, are fine.
- Each term is 3–200 characters on one line, with at most 1000 terms per list.

## Corpus gate

`corpus.json` (`projectbuddy/redaction-corpus@1`) is **synthetic**. It names
operator `denyTerms` and `wholeWordTerms`. Each item lists strings that must not
survive and strings that must stay, and every item must also be idempotent.
Token-shaped values are written as `{{templates}}` and assembled at test time,
so no token-like literal is ever committed.

Run it with `dotnet test ProjectBuddy.PeerRedact.Tests`. CI requires 100% line
and branch coverage.
