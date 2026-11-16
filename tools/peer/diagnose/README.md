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
   against its `data` object. A cited string also matches a number or boolean
   whose JSON text it is exactly, so `"5"` cites `5`.
5. **Repair.** A rejected answer is shown its reason and may answer again, up
   to `maxAnswerAttempts`. After that the tool gives **no advice** (exit 6) and
   never a guess.

Every request must provably fit half the context (`TruncationGuard.Fits`). If
one does not, nothing from the run is trusted (exit 6).

## Policy (`policy.json`, `projectbuddy/diagnose-policy@1`)

The policy pins the base model `qwen2.5:7b` by its registry digest. This is the
model #217 chose, and its digest is the same on every OS, unlike a locally
built identity. It also pins:
- the context, output budget and seed (temperature is always 0);
- the loop's bounds;
- the prompts;
- the closed list of **questions**.

Each question's text states its classes and the rule for each class, so the
class follows from tool values rather than from the model's arithmetic. For
example, `disk` reports `freePercent` and `disk-low` means a drive below 10.
Every question has `inconclusive`. The policy's SHA-256 goes into the report.

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
 "verified":["citations"],
 "answer":{"class":"disk-low","finding":"…","citations":[{"tool":"disk","pointer":"/data/drives/0/freePercent","value":5}]},
 "evidence":[{"tool":"disk","arguments":{},"sha256":"…"}],
 "model":{"name":"qwen2.5:7b","digest":"…"},"ollamaVersion":"…","policySha256":"…",
 "receipt":{"numCtx":16384,"probePromptTokens":8194,"requests":3,"rounds":2,"toolCalls":1,"refusedCalls":0,"answerAttempts":1,"evidenceBytes":306,"maxPromptTokens":758,"outputTokens":185,"seconds":5.2}}
```

- No tool output leaves except the cited values, which were already redacted.
  `evidence` holds only each accepted call and the SHA-256 of the result it
  returned.
- The answer is advisory. Only the citations are verified; the class and the
  finding are the model's judgment.

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
