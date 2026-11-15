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
  (`RedactionException`), never partly redacted, unless it is already redacted.
  A placeholder is at most three times the shortest text it replaces
  (`abc` → `<private>`), so output can grow to `Redactor.MaxOutputChars`
  (12,000,000). Text up to that size that is already settled is returned
  unchanged, so redacting redacted text changes nothing at any size.
- Invisible format characters are removed, including those outside the Basic
  Multilingual Plane (zero-width spaces and joiners, soft hyphens, tag
  characters), and lone surrogates become `U+FFFD`. Nothing a reader cannot see
  can split a term or an address.
- Inside a **valid JSON string literal** (every escape valid), `\uXXXX` escapes
  become their shortest JSON spelling. The default .NET `Utf8JsonWriter` and
  Windows PowerShell 5.1 `ConvertTo-Json` escape `'`, `"`, `<`, `>`, `&` and every
  non-ASCII character.
  - Printable characters are written literally, and a quote and a backslash as
    `\"` and `\\`. The literal's JSON meaning is unchanged, but `\u0027D:/Clients`
    or `M\u00FCller` can no longer hide a path or a term.
  - Escapes of invisible characters at a deeper level (JSON inside JSON) are
    removed.
  - Text outside a valid literal is never decoded, so a raw Windows path such as
    `C:\Temp\uadd1` keeps its separator.
- Invisible characters are removed again and the text is normalized to NFC.
  Unicode noncharacters (`U+FDD0`–`U+FDEF`) are removed too; the masks below use
  them, so evidence can never forge one.
- The rules below run in order, and the whole order, cleaning included,
  **repeats until the text stops changing**. A later pass is needed only when one rule's placeholder
  unlocks an earlier rule, so `Redactor.MaxPasses` (32) exceeds the number of
  rules: even a deliberate chain through every rule settles. Text that has not
  settled by then is refused. Idempotence therefore holds by construction. A
  seeded fuzz of 300,000 glued-together fragments never needed more than 6
  passes.

## Rules, in order

| Rule | Becomes | Kept |
|---|---|---|
| Private keys; known token formats (GitHub, GitLab, cloud, chat, JWT, npm, Google API) | `<secret>` | — |
| `Authorization`/`Proxy-Authorization` values, plain or in escaped JSON (`\"Authorization\":\"…\"`), after `:`, `=` or a table gap of 2+ spaces, to the closing quote or the end of the line | `<secret>` | the header name and a well-known scheme (`Bearer`, `Basic`, `token`, `AWS4-HMAC-SHA256`, …) |
| Token-like `Bearer`/`Basic` credentials elsewhere (16+ characters with a digit) | `<secret>` | the scheme; prose such as "Basic authentication" stays |
| XML `key="…Password…" value="…"` | `<secret>` | the key |
| `reg query` string and binary rows whose name holds a secret word or ends in `pass`/`pw` (`DefaultPassword    REG_SZ    …`, `ProxyPass    REG_SZ    …`), and `netsh` `Key Content : …`, to the end of the line | `<secret>` | the name and type; numeric rows (`DisablePasswordChange    REG_DWORD    0x0`) stay |
| Command lines (an argument may mix plain and quoted parts, and an unclosed quote runs to the line end): the password argument of `net use` (first plain argument) and `net user` (second, after the account, which becomes `<user>`); `schtasks /P`, `/RP`; `sqlcmd`, `bcp`, `osql` `-P` (also glued, `-PSecret`); `psexec`, `certutil` `-p` (and `/p`), `docker login` `-p`; `7z -pSecret` and `-p"two words"`. The matching account switches (`/U`, `/RU`, `-U`, `-u`) become `<user>`. Short flags count only after their own command, because a general `-p` is a port. | `<secret>` | the command and its switches |
| PowerShell: `ConvertTo-SecureString "…"`, `-String …` and `"…" \| ConvertTo-SecureString`; `NetworkCredential("user", "…")`, `-ArgumentList "user", "…"` and `::new($user, "…")` (a literal user becomes `<user>`, a variable stays) | `<secret>` | the call |
| Flags: `--token x`, `-pw x`, `--password=x`, `-passin`/`-passout pass:x`, `/pass:x`, `/password:x` | `<secret>` | the flag |
| Assignments whose key contains a secret word, with any prefix (`DB_PASSWORD`, `ACTIONS_RUNNER_INPUT_TOKEN`, `"api_key"`, `\"password\"`), or whose key is `pw` or `pass` as a whole word part (`$pw`, `db_pass`, but not `bypass`; a number after them is a count, as in `Pass: 9`, so an unquoted numeric PIN there stays visible), after `:`/`=` or a table gap other than a `reg query` type column. A quoted value (plain or escaped) is taken whole. After a `;` (a connection-string item), the value runs to the next `;`, the line end or a quote; elsewhere a `;` ends it. Both are capped at 256 characters. A value that starts with a credential cmdlet (`$pw = Read-Host …`) is code and stays. | `<secret>` | the key and separator |
| SAS signatures (`?…&sig=…`, also after an escaped `&` in JSON inside JSON) | `sig=<secret>` | the other query fields |
| Accounts: `/user:x`, `/u:x`, `--user=x`, `--username x`, `User Id=x`, `UserName=x` | `<user>` | the switch or key |
| URLs, plain or JSON-escaped (`https:\/\/`) | `<url>` | `scheme://host[:port]` for allow-listed hosts, with the path **always** replaced by `/<path>`; user info is dropped |
| `[user@]host:path` git remotes (user-less only when the path ends in `.git`), e-mail addresses | `<url>`, `<email>` | image digests (`alpine@sha256:…`) |
| Drive paths in every spelling: `X:\`, `X:/`, JSON-escaped `X:\\`, `file:///X:/`, `X%3A%2F`, `\\?\X:\`, `\\.\X:\` | `<path>` | allow-listed prefixes to a fixed depth (below) |
| UNC and device paths (`\\server\share`, `\\?\UNC\…`, `\\.\pipe\…`, `//server/share`, `file:////server/share`); Git Bash, WSL and Cygwin drive paths (`/c/…`, `/mnt/c/…`, `/cygdrive/c/…`) | `<path>` | — |
| User profiles (`C:\Users\x`, `/home/x`, `/Users/x`, any case) | `<user>`, and the rest `<path>` | allow-listed profile folders to a fixed depth (below) |
| MAC addresses; IPv4/IPv6 outside the allowed networks, including one that ends a sentence | `<mac>`, `<ip>` | loopback, the unspecified address and the cable `10.211.0.0/30`; versions such as `Version=13.0.0.0`, `"ProductVersion": "1.0.0.0"` and `v1.2.3.4` |
| Directory names and certificate subjects, read as a whole sequence in LDAP (`CN=x,OU=y,DC=z`), curl (`C=US; O=x; CN=y`), OpenSSL (`C = US, O = x`, `/C=US/O=x`) and `certutil -dump` (one attribute per indented line after `Issuer:`) form. Any attribute may follow a known one (`SERIALNUMBER=`, `OID.2.5.4.5=`), a value may be quoted when it holds a comma (`O="Contoso, Inc."`), and a line break after `,` continues the sequence. A sequence counts with `CN`, `OU` or `DC`, with `O` beside another attribute, or after `Subject:`/`Issuer:`, so `retries=3, s=5` stays. | `<private>` per value | the country (`C=US`), allow-listed hosts (`CN=github.com`) and public issuers: a known certificate authority or vendor name (`DigiCert`, `GlobalSign`, `Let's Encrypt`, `Microsoft`, …) followed only by certificate-naming words and an optional final dot (`Microsoft Code Signing PCA 2011`, `DigiCert Global Root G2`, `GitHub, Inc.`), so `Microsoft Teams Room - Contoso` stays private. When every `CN` and `O` is a public issuer, its `L`, `S` and `ST` stay too |
| Bare host names (below) | `<host>` | allow-listed hosts, again with any path replaced by `/<path>` |
| The account in `user@<ip>` or `user@<host>` (`ssh jane@10.0.0.5`) | `<user>` | — |
| Deny-list terms (case-insensitive, longest first, words joined by any whitespace) | `<private>` | placeholders are never rewritten |

**What is deliberately kept,** because diagnosis needs it: hex digests and hashes,
version and build numbers, exit codes, times, file names such as `main.vi` or
`ollama.exe`, .NET namespaces (`System.IO`, `Microsoft.NETCore.App`, `ASP.NET`),
Windows `.com` programs under `System32` and `SysWOW64` (`chcp.com`), and
PowerShell `::` calls.

A rule never takes a value that is already a placeholder again. A quoted value
keeps its quote spelling (`{"password":"<secret>"}`, `$pass = '<secret>'`), so
JSON stays well formed, and a .NET type name
(`"Credential":"System.Management.Automation.PSCredential"`) is not a secret.

### Name/Value layouts

Diagnostics often keep a secret's name and its value in sibling fields rather
than as `key=value`. When the name holds a secret word (the assignment keys, plus
`credential`), the value becomes `<secret>`:
- a flat JSON object's `Name`/`Key` member and its `Value`, `VariableValue` or
  `Data` member (`Get-ChildItem env: | ConvertTo-Json`, `Win32_Environment`,
  `{"key":"ClearTextPassword","value":"…"}`). These objects and JSON argument
  arrays are read before escaped strings are masked;
- a Format-List or YAML record: `Name : DB_PASSWORD` (or `- name: …`) followed,
  within eight lines, by `Value :`, `VariableValue :` or `Data :`;
- a CSV table whose header row names a `Name`/`Key` column and a
  `Value`/`VariableValue`/`Data` column (`ConvertTo-Csv`), row by row;
- a JSON argument array (`["tool.exe","--password","…"]`), where the element
  after a secret flag, or after a command's own password switch
  (`["sqlcmd",…,"-P","…"]`), is secret, and the element after an account switch is
  `<user>`.

## JSON evidence

Evidence is often JSON: process command lines exported with `ConvertTo-Json`,
event records, or the agent's own `Utf8JsonWriter` output, sometimes nested
inside another JSON envelope.

- **A JSON string literal with escapes, between JSON punctuation** (after `{`,
  `[`, `:` or `,` and before `:`, `,`, `]` or `}`), is redacted as the text it
  stands for. It is decoded, so `\n` is a real line break and `\"` a quote, then
  settled with every rule (nested JSON included, at any depth), and encoded
  again. Line-based rules therefore work inside JSON exactly as in raw text, and
  the output stays well formed.
- While the other rules run over the rest of the text, such a literal is held
  behind a quoted mask, so a value rule (`"password":"…"`) still takes it whole
  and nothing reads its escapes as path separators.
- A quoted raw path is not decoded and stays with the path rules. That covers a
  path not between JSON punctuation (`copy "C:\temp\new"`), and also any literal
  that holds a raw drive or UNC path: a standalone drive colon, or a server name
  at the start of a path, followed by a single backslash escape
  (`path: "D:\repos\fabrikam", exit: 1`). An encoder always doubles the
  backslash there.
- Outside such literals (escaped JSON shown in a log line), a quoted value closes
  only with its own quote spelling: `"x"`, `\"x\"`, `\\\"x\\\"`,
  `\\u0022x\\u0022` or `\u0022x\u0022`.
- An unquoted value keeps a backslash with the next character and stops at the
  close of a JSON string (`"` or `\"` before `,`, `:`, `]`, `}` or the line end),
  so a placeholder never splits a string or swallows its closing quote. A quoted
  command argument whose closing quote lies there keeps its opening quote
  (`"<secret>"`).
- UNC roots are recognized at any depth (`\\fs01`, `\\\\fs01`, …), and the
  secret, account and command rules also start right after a `\uXXXX` escape
  left at a deeper level.

## Paths

A drive starts after a non-alphanumeric character, a JSON `\t`, `\n` or `\r`
escape, or a short switch (`-oD:\x`, `/ID:\x`). Never inside a word, so
`https://` is not a drive `s:`. UNC, `//server` and POSIX paths also start
right after a short switch (`-o\\fs01\x`, `/I\\srv\x`, `-o/home/x`).

A path match runs on through spaces and apostrophes (`O'Brien`, `Customers' Data`), so
`D:\Acme Projects\x` never leaks its tail. A path opened by an apostrophe
(`'D:\x'`, as in PowerShell errors) ends at one, except a doubled `''`
(PowerShell's escape), one before a letter or digit, one followed by more path
(`\` or `/`) before the next apostrophe (`'D:\Customers' Data\x'`), or one whose
next apostrophe closes the quote before a lower-case word or the line end
(`'D:\Jones' Bakery.docx' is denied`). It stops:
- at a double quote, `<`, `>`, `|`, a tab, a line end or a wildcard;
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
`pwd`; whitespace runs before digits; `/O=a` and `Subject: s=1, `; `sqlcmd -P a`
and `7z -p"a`; unclosed JSON literals and argument quotes; `D:\a' ` and
`'D:\a' b\`; attributes on indented lines; and backslash runs before `u0027`.

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
Every item is also wrapped in JSON three ways (`System.Text.Json` at one and two
levels, and PowerShell 5.1 style with `\"` escapes). Nothing listed may survive
in the decoded value, the output must still parse at every level, and
redaction must stay idempotent. An item whose input is itself JSON must still
parse after redaction.
Token-shaped values are written as `{{templates}}` and assembled at test time,
so no token-like literal is ever committed.

Run it with `dotnet test ProjectBuddy.PeerRedact.Tests`. CI requires 100% line
and branch coverage.
