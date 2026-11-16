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
- **JSON objects at any depth:** an object's `Name`/`Key` member and its `Value`,
  `VariableValue` or `Data` member. Examples are `Get-ChildItem env: |
  ConvertTo-Json`, whose items also hold nested `PSDrive` and `PSProvider`
  objects, `Win32_Environment`, and `{"key":"ClearTextPassword","value":"…"}`.
  - One linear scan reads them, and it also tracks strings, nesting and
    truncated JSON (a cut string runs to the line end).
  - Outside a value position, a string counts only when JSON punctuation (or the
    line end) follows its closing quote. So a stray quote in prose or a command
    line (`cmd /c "C:\scripts\" deploy.cmd {…}`) cannot shift how later quotes
    pair. Once a quote finds no closing quote on its line, later quotes on that
    line are not scanned again, so the scan stays linear.
  - Frames are always finished before they are dropped. A stray closer, the end
    of the text, or the depth cap (past 256 the scan starts over, so memory stays
    bounded) never skips a Name/Value pair read so far.
  - JSON is read before escaped strings are masked.
- **Format-List and YAML records:** `Name : DB_PASSWORD` (or `- name: …`), with
  `Value :`, `VariableValue :` or `Data :` within eight lines, in either order.
- **XML:**
  - An element named for a secret: `<password>…</password>` (Maven) or
    `<AdministratorPassword><Value>…</Value>` (unattend.xml, whose secret
    elements hold a `<Value>` child).
  - An element whose `Name`, `N` or `Key` attribute names one:
    `<Property Name="Password">…</Property>` (`ConvertTo-Xml`), `<S N="Password">…</S>`
    (CLIXML), or a `<value>` child of `<setting name="ApiToken">` (.NET settings).
  - A `name="…" value="…"` pair, as well as `key="…" value="…"`: Spring
    `<property>`, `.cscfg` `<Setting>`, `<add name=…>`.
  - A `<Value>` seen without its parent, as `grep -A1`, `Select-String -Context`
    or `Get-Content | ConvertTo-Json` show it. That means a `<Value>` right after a
    secret-named element (no other tag between, within 300 characters) or one
    followed by unattend.xml's `<PlainText>`. A `<Value>` seen alone
    (`Select-String` without context) carries no hint and stays.
  - Name/Value siblings in either serializer: `<S N="Name">DB_PASSWORD</S>`
    followed, within eight elements, by `<S N="Value">…</S>`. Only this order is
    read; it is the order PowerShell writes.
  - Tag attributes are read up to 1000 characters, so an unclosed tag in prose
    (`count<limit`) never makes the scan run on.
  - A `true`/`false` value is a setting, not a secret
    (`<RequirePassword>true</RequirePassword>`). Any other value of a secret-named
    element is hidden, numbers included (`<PasswordExpires>2026</PasswordExpires>`).
- **PowerShell object strings:** in `@{Name=DB_PASSWORD; Value=…}`, the value
  beside a secret name is hidden, and so is any member keyed by one
  (`@{Password=a b; User=x}`).
- **Printed dictionary entries:** `[NAME, value]` (CLIXML's
  `<ToString>[DB_PASSWORD, …]</ToString>`, Format-List's
  `Environment : {[PATH, …], [DB_PASSWORD, …]}`). The value runs to the closing
  `]` without crossing another bracket, so a value holding `[` or `]` is cut there.
- **CSV tables** whose header row names a `Name`/`Key` column and a
  `Value`/`VariableValue`/`Data` column (`ConvertTo-Csv`, `wmic … /format:csv`
  with its `\r\r\n` line ends). A row with more fields than the header holds a
  comma in an unquoted value, so everything from the value column on is secret.
- **JSON argument arrays,** where the command is the first element or a sibling
  `FileName`/`Path`/`Executable` member (`{"FileName":"sqlcmd.exe","Arguments":[…]}`).
  - The element after a secret flag (`["tool.exe","--password","…"]`), or after
    the command's own password switch (`["sqlcmd",…,"-P","…"]`), is secret.
  - The element after an account switch is `<user>`.
  - `docker`'s `-p` and `-u` count only after the `login` subcommand, which may
    follow global options (`docker --context prod login -p …`). In `docker run`
    they are a port and a container user.
  - `ProcessStartInfo.Arguments` is a single string. In JSON
    (`{"FileName":"sqlcmd.exe","Arguments":"-S db01 -U sa -P …"}`) and in a
    Format-List record (`FileName :` and `Arguments :` within sixteen lines, in
    either order), it is read as the `FileName` program's command line.

Secret names also include:
- `passphrase`;
- a final `_PAT` part (`AZURE_DEVOPS_EXT_PAT`);
- a secret word with a value suffix (`$passwordText`, `tokenValue`);
- docker config's `"auth"`;
- **any** separated `_KEY` name (`AZURE_OPENAI_KEY`, `COSMOS_KEY`, `NUGET_KEY`),
  except a deny-list of keys that name data, not secrets. Examples are `cache`,
  `sort`, `partition`, `hash`, `range`, `foreign`, `index`, `routing`, `shard`,
  `project`, `registry`, `public`, `idempotency`, `object` and `message`
  (`cache_key`, `PROJECT_KEY`, `Public-Key`). Failing open on an unknown
  qualifier would leak, so the list names what is safe;
- a camelCase `…Key` only with a secret qualifier (`storageKey`, `primaryKey`,
  `secondaryKey`, `apiKey`).

An assembly's `PublicKeyToken` is public.

Other value rules:
- **Line-start keys:** a line can start with an unquoted secret key, after an
  optional log prefix. Prefixes are an ISO or Python timestamp with an optional
  level, as on every GitHub Actions log line, and a bracketed tag such as the
  runner's `_diag` `[2026-11-15 10:00:00Z INFO Runner]`. Examples are
  `GPG_PASSPHRASE=…`, `export X=…`, `set X=…`, Dockerfile `ENV X=…` and
  `ENV X …`, and YAML `password: …`.
  - Its value takes the rest of the line, quotes and brackets included, so a
    value with spaces, an unclosed quote or a `]` never leaks a word.
  - A value that is exactly one quoted string keeps its quotes; anything after it
    on the line (`"abc" # note`) is taken too.
- **Table rows:** a two-column row (`Get-ChildItem env:`, `set`), with a secret key
  and a gap of two or more spaces, takes the rest of the row when no second gap
  follows. So kubectl's `db-password   Opaque   1   5d` and a header
  `Token   Type   Expires` are not rows. A key in the middle of a line, or a
  row whose value holds a double space, falls back to the first word after the
  gap. The short keys `pw`, `pass` and `pat` never use a gap (Jest's
  ` PASS  src/…`).
- **Tools that set a secret:** `setx NAME value`,
  `[Environment]::SetEnvironmentVariable("NAME", "value", …)`,
  `Set-Item env:NAME value`, `New-Item -Path env:NAME -Value …`,
  `gh secret set NAME --body …` and `az keyvault secret set … --value …`.
  - The value never starts with `=` or `|`: `$env:X = "y"` is an assignment
    (the assignment rules take it), and `Get-Item env:X | …` is a pipeline.
  - The value never takes the quote that closes a JSON string.
- **Quoted keys:** a secret key that opens a quoted string takes the value to the
  closing quote of the same spelling. Examples are `"GPG_PASSPHRASE=a b c"` in
  `docker inspect`'s `Env`, `docker run -e "X=a b"`, and `\"X=a b\"` or `'X=a b'`.
- **Object and array values:** an assignment never takes a value that starts
  with `{` or `[`; nested JSON is the scanner's.
- **JSON data, not secrets:** after a quoted key, a JSON number, boolean or `null`
  is data and stays: `"secret": 0` (a redaction count), `"hasToken": true`,
  `"token": null`, and a Name/Value object's numeric `Value` (an event count under
  a provider named `…TokenBroker…`). Any other bare value is written as a string
  (`"<secret>"`), so the JSON still parses.
- **VirtualBox machine-readable keys:** a secret key in brackets, in the raw
  `showvminfo --machinereadable` line only (`vrdeproperty[VNCPassword]="…"`).
  The same key as a JSON member (`{"vrdeproperty[VNCPassword]":"…"}`) is not
  read. An unseparated MAC is read only under a `macaddressN` key
  (`macaddress1="080027A1B2C3"` → `<mac>`); under `"mac"` or `"physicalAddress"`
  it stays. Tools must allow-list exact keys (#292).
- **Numbers after a quoted key:** a numeric secret written as a JSON number
  (`{"password":123456}`) stays, the cost of keeping counts readable (#293).

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
- A quoted raw path not between JSON punctuation (`copy "C:\temp\new"`) is not
  decoded and stays with the path rules.
- A literal that may hold a raw drive or UNC path is read both ways: decoded and
  redacted, then left unmasked so the path rules read it raw as well. Such a
  literal has a standalone drive colon, or a server name at the start of a path,
  followed by a single backslash escape (`path: "D:\repos\fabrikam", exit: 1`);
  an encoder always doubles the backslash there. Either reading can only add
  redaction.
- Outside such literals (escaped JSON shown in a log line), a quoted value closes
  only with its own quote spelling: `"x"`, `\"x\"`, `\\\"x\\\"`,
  `\\u0022x\\u0022` or `\u0022x\u0022`.
- An unquoted value keeps a backslash with the next character and stops at the
  close of a JSON string (`"` or `\"` before `,`, `:`, `]`, `}` or the line end)
  or of a JSON object or array (`null}`),
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
each adversarial shape and require it to finish within 30 seconds (the slowest
takes 8 seconds). These `Category=Performance` tests run in Release in their own
CI step; the coverage run, which instruments every branch, leaves them out.
The shapes include repeated `a.`, `C:\` and `password=a `; `key="` followed by
`pwd`; whitespace runs before digits; `/O=a` and `Subject: s=1, `; `sqlcmd -P a`
and `7z -p"a`; unclosed JSON literals and argument quotes; `D:\a' ` and
`'D:\a' b\`; attributes on indented lines; backslash runs before `u0027`;
escaped JSON in a log line (`msg="{\"k\":\"v\",…`); an unclosed tag full of
`N="Name"` attributes; `[DB_PASSWORD, x` without a closing bracket; and prefixed
log lines.

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
