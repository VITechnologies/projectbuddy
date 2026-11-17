# `diagnose/`: `projectbuddy-diagnose`

`projectbuddy-diagnose` is the bounded agent loop of stage P4
([#295](https://github.com/VITechnologies/projectbuddy/issues/295), slice 3 of
[#290](https://github.com/VITechnologies/projectbuddy/issues/290) in
[#210](https://github.com/VITechnologies/projectbuddy/issues/210)). A local
model answers one question from a closed list about the machine it runs on.
It may call only the read-only tools of [`../probes`](../probes/README.md).
Its answer is trusted only when every fact it cites is found, exactly, in a
tool result the model was given.

It is a .NET 10 single-file program, like `projectbuddy-advise`. It uses the
shared guard library [`../ollama`](../ollama/README.md), the redaction in
[`../redact`](../redact/README.md), and the probes registry as a library.

```bash
dotnet publish ProjectBuddy.PeerDiagnose -c Release -r win-x64 -o out
out/projectbuddy-diagnose.exe --policy policy.json --question github-unreachable --deny-list <private deny list>
```

## How a question is answered

1. **Guard.** The tool checks that the pinned model is installed with its
   digest. It then runs the `num_ctx/2` truncation probe, so every later
   request can be proven to fit.
2. **Loop.** The model gets the policy's system prompt, the question, and the
   registry's tools as native Ollama tool definitions.
   - Each round, it may call tools or stop.
   - Each call is answered by `Registry.InvokeAsync`, which returns a redacted,
     capped result envelope, or with a one-line refusal the model can read.
   - Calls are refused for an unknown tool, bad arguments, more than
     `maxCallsPerRound` calls in a round, more than `maxCalls` calls in total,
     or a result that would go over `maxEvidenceBytes`.
   - A repeated call returns its first result, so one question sees one machine
     state.
   - The loop ends when the model stops calling tools, or after `maxRounds`.
3. **Answer.** The answer is asked for separately, without tools, constrained
   by a JSON schema to `{class, finding, citations}`.
4. **Grounding.** The answer is redacted again and then checked:
   - `class` must be one of the question's classes;
   - `finding` must be 1 to `maxFindingChars` characters;
   - there must be 1 to `maxCitations` citations, each exactly
     `{tool, pointer, value}` with a scalar value of at most `maxValueChars`
     characters;
   - each `pointer` (RFC 6901) must resolve to exactly the cited `value` in a
     result of that `tool` the model was given.

   A pointer is resolved against the result as the model received it, then
   against its `data` object. Values are compared by their JSON text and are
   never converted: a string by its content, a number or boolean by its exact
   token (`12.0` does not cite `12`). A cited string also matches a number or
   boolean whose JSON text it is, so `"5"` cites `5`, and an empty list or
   object is cited as `"[]"` or `"{}"`. Model text in a
   rejection reason is shown as JSON, with control characters escaped, and is
   cut short.
5. **Class.** The question's **rules** decide the class from the same
   evidence (see [Class rules](#class-rules-299)).
   - When they decide, the answer schema allows **only** that class from the
     first attempt (#302). The model's class is checked against it anyway, as
     a defence in depth. The report says `"verified": ["citations", "class"]`,
     with the rule and the facts it rests on.
   - The answer prompt also shows the model those facts, so it can cite them
     exactly (#304). For a rule with no facts to show (such as "no element
     exists"), the prompt shows the rule itself.
   - When the rules cannot decide, the class is free, stays the model's
     judgment, and only the citations are verified.
6. **Repair.** A rejected answer is shown its reason and may answer again, up
   to `maxAnswerAttempts`. After that the tool gives **no advice** (exit 6) and
   never a guess.

Every request must provably fit half the context (`TruncationGuard.Fits`). If
one does not, nothing from the run is trusted (exit 6).

## Policy (`policy.json`, `projectbuddy/diagnose-policy@2`)

The policy pins the base model `qwen2.5:7b` by its registry digest. This is the
model #217 chose, and its digest is the same on every OS, unlike a locally
built identity. It also pins:
- the context, output budget and seed (temperature is always 0);
- the loop's bounds;
- the prompts;
- the closed list of **questions**.

Each question has its text, its classes (always including `inconclusive`),
and its **rules**. The policy's SHA-256 goes into the report.

### Class rules (#299)

The model reads the rules as prose that is generated from them, so the prompt
and the check cannot drift. Rules are tried in order, and the first that holds
decides the class.

```json
{"class": "disk-low", "all": [
  {"tool": "disk", "pointer": "/data/drives/*", "quantifier": "some",
   "test": [{"pointer": "/freePercent", "op": "lt", "value": 10}]}]}
```

- **Rule:** `{class, all: [conditions]}` or `{class, any: [conditions]}`. An
  empty `all` always holds, so it serves as "otherwise".
- **Condition:** `{tool, pointer, quantifier, test}`.
  - `pointer` is RFC 6901 into the tool result, and `*` steps over every
    element of an array.
  - `quantifier` is `some`, `every` or `none`. It says whether some, every or
    no element passes **all** its tests.
  - `every` over no elements is false, so there is no vacuous truth. `none`
    over no elements is true.
- **Test:** `{pointer, op, value}`, where the pointer is relative to the
  element and `""` means the element itself.
  - `eq` and `ne` compare scalars by their JSON text, as citations do.
  - `lt`, `le`, `gt` and `ge` compare numbers.
  - `prefix` checks that a string starts with the value.
  - `present` checks that the pointer resolves, and takes no value.
- **Undecided:** a condition is undecided in three cases:
  - its tool was not called;
  - a result of that tool failed;
  - the result was truncated and the outcome depends on items that may have
    been cut (`some` with no match, `none`, or an `every` that held).

  Rules combine in three values. An undecided rule before the deciding one,
  or no rule holding, leaves the class undecided.
- **Parse checks:** an empty `any` (which never holds) and any rule after an
  "otherwise" rule (which is unreachable) are refused.
- **Failures inside data:** some probes report a failed read inside a normal
  result: `services` sets `error`, `event-errors` lists `errors`, and
  `vm-states` gives `running: null`. The shipped rules test for these with
  explicit `inconclusive` rules placed before any rule they could mislead.
  `healthy` also requires `services` to be available with no error.
- Tests run the **shipped policy** on probe-shaped results, failures
  included, so it decides only what the evidence supports.

| Question | Classes |
|---|---|
| `github-unreachable` | `no-active-link`, `no-default-route`, `link-up`, `inconclusive` |
| `runner-offline` | `runner-service-missing`, `runner-service-stopped`, `runner-service-running`, `recent-crash`, `inconclusive` |
| `ollama-empty` | `ollama-unreachable`, `no-models`, `identity-missing`, `identity-present`, `inconclusive` |
| `vm-states` | `virtualbox-missing`, `no-vms-visible`, `vms-running`, `no-vm-running`, `inconclusive` |
| `disk-full` | `disk-low`, `disk-ok`, `inconclusive` |
| `general-health` | `healthy`, `degraded`, `inconclusive` |

## Report (`projectbuddy/peer-diagnosis@1`)

```json
{"schema":"projectbuddy/peer-diagnosis@1","tool":"projectbuddy-diagnose 0.1.0","advisory":true,"question":"disk-full",
 "verified":["citations","class"],
 "answer":{"class":"disk-low","finding":"…","citations":[{"tool":"disk","pointer":"/data/drives/0/freePercent","value":5}]},
 "rule":{"class":"disk-low","basis":[{"tool":"disk","pointer":"/data/drives/0/freePercent","value":5}]},
 "evidence":[{"tool":"disk","arguments":{},"sha256":"…"}],
 "model":{"name":"qwen2.5:7b","digest":"…"},"ollamaVersion":"…","policySha256":"…",
 "receipt":{"numCtx":16384,"probePromptTokens":8194,"requests":3,"rounds":2,"toolCalls":1,"refusedCalls":0,"answerAttempts":1,"evidenceBytes":306,"maxPromptTokens":758,"outputTokens":185,"seconds":5.2}}
```

- No tool output leaves except the cited values, which were already redacted.
  `evidence` holds only each accepted call and the SHA-256 of the result it
  returned.
- The answer is advisory.
  - The citations are always verified.
  - The class is verified when the rules decide it. `rule.basis` lists up to 8
    facts it rests on.
  - The finding is the model's judgment.

| Exit code | Meaning |
|---|---|
| 0 | Report written |
| 2 | Usage error |
| 3 | Refused: an invalid or unreadable policy, an unknown question, an unreadable or invalid deny list, a non-loopback `--ollama`, or the model not installed with its pinned digest |
| 4 | The truncation probe failed |
| 5 | Ollama was unreachable |
| 6 | **No advice**: a request did not provably fit, or the answer was not grounded after `maxAnswerAttempts` |

`--vboxmanage` must be a fully qualified path. `vm-states` sees only the VMs
registered to the account that runs the tool.

## Asking from the workstation (`projectbuddy-ask diagnose`, #298)

The cloud agent reaches the peer's diagnostics agent through the existing
`projectbuddy-ask` forced-command channel, which is the same key and audit
trail as the other verbs.

```bash
projectbuddy-ask ask diagnose disk-full --host <peer> --user <account> --identity <key> --known-hosts <file>
```

- **Question ID:** a letter or digit, then `[a-z0-9-]`, at most 64
  characters. It cannot start with `-` and cannot carry a trailing newline.
  Only the peer's pinned policy decides whether the question exists; an
  unknown question is refused (exit 64 on the peer, 3 on the client).
- **Paths on the peer (`serve`):** `projectbuddy-diagnose` runs from fixed
  paths, with no shell, and has 180 s.
  - The program and `policy.json` are in
    `%ProgramData%\ProjectBuddy\peer-ask\diagnose\`.
  - The deny list is
    `%ProgramData%\ProjectBuddy\peer-link\redaction-denylist.json`.
- **Answer:** the `projectbuddy/peer-ask@1` envelope.
  - A grounded answer is `{"question", "advice": true, "report"}`.
  - No advice is a normal answer, `{"question", "advice": false, "reason"}`.
    The cloud agent then looks for itself; it is not an error.
- **Client checks:** the client waits 200 s and checks the answer's question,
  its `advice`, and either the report's schema and verified citations or the
  reason.
- **Account:** the agent runs as the SSH account, so `vm-states` sees only
  that account's VMs.

The verb list in [`../README.md`](../README.md) is pinned content of the
`projectbuddy` identity (`identity/identity.json`). It lists `diagnose` from
identity 0.2 (#358).

## Tests

```bash
dotnet test ProjectBuddy.PeerDiagnose.Tests --settings coverage.runsettings --collect:"XPlat Code Coverage"
```

The tests use a scripted Ollama, including native tool calls, and a fixed
machine. They cover:
- the policy and every refusal;
- every grounding rule and pointer form;
- the call, round and evidence budgets, and repeated calls;
- the guard on every request;
- the repair turn;
- redaction of the answer;
- the command line.

CI adds the tool to the `Peer Tool Tests` matrix at 100% line and branch
coverage and publishes the trimmed win-x64 binary.
