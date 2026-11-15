# `advise/`: `projectbuddy-advise`

`projectbuddy-advise` is the stage P2 tool
([#284](https://github.com/VITechnologies/projectbuddy/issues/284) in
[#210](https://github.com/VITechnologies/projectbuddy/issues/210)). It gives a
cloud agent a few hundred bytes of local, advisory triage instead of a whole
CI log, which serves D13 and D14 in #210. It is a self-contained .NET 10
single-file program, like `projectbuddy-ask` and `projectbuddy-bench`, and it
uses the shared guard library in [`../ollama`](../ollama/README.md).

```bash
dotnet publish ProjectBuddy.PeerAdvise -c Release -r linux-x64 -o out
gh run view <run-id> --repo <owner/repo> --log-failed \
  | out/projectbuddy-advise triage --policy policy.json --ollama http://127.0.0.1:11434
```

- `--input <file>` reads the log from a file instead of stdin.
- `--ollama` must be `http://` followed by a loopback IP literal and a port.
  The client ignores proxy settings and refuses redirects.
- `--timeout-seconds` bounds each request. The default is 300 and the
  maximum 3600.
- On success the report goes to stdout. On failure **nothing** goes to stdout:
  only a one-line reason goes to stderr, and the exit code says why.

| Exit code | Meaning |
|---|---|
| 0 | Report written |
| 2 | Usage error |
| 3 | Refused input: invalid policy, an empty log, an unreadable file, or the model is not installed with its pinned digest |
| 4 | The truncation probe failed, so the server no longer truncates the way the guard assumes |
| 5 | Ollama was unreachable or returned an invalid response |
| 6 | **No advice**: the evidence never provably fit, or the answer was not grounded in the evidence |

## Policy (`policy.json`, `projectbuddy/advise-policy@1`)

The policy pins:
- the model, by full manifest digest;
- `numCtx`, `numPredict` and the seed (temperature is always 0);
- `maxInputBytes`;
- the triage prompt and its closed class list.

Each report records the policy's SHA-256. The committed policy uses the base
model `qwen2.5:7b`, which won the #217 benchmark, at `numCtx` 16384. The
`projectbuddy` identity is not used here, because its system prompt alone
costs about 2800 tokens.

## What the model sees

1. **Normalization.**
   - ANSI codes and per-line timestamps are removed.
   - `gh run view --log` rows become one `== job / step ==` header per
     section, instead of a prefix on every line.
   - Runs of blank lines collapse, and lines are clipped at 2000 characters.
2. **Error-looking lines first.** Up to 40 distinct lines matching words such
   as `FAIL`, `error`, `fatal`, `exception` or `exit code N` are collected
   from the **whole** log, newest first, within a third of the budget. A
   failure buried among hundreds of passing checks, or one that falls before
   the tail, still reaches the model.
3. **The tail.** The newest whole lines that fit the rest of the budget,
   prefixed with the header of the section they start in.

## Guards (fail closed)

- **Digest pin.** The model must be installed with exactly the pinned digest.
- **Truncation probe.** Ollama keeps only the last `num_ctx/2 + 2` tokens of
  an oversized prompt and drops the system prompt with them, without
  reporting it. Before any triage, a probe proves the server still behaves
  that way. If it doesn't, the tool exits 4.
- **Accept rule.** A reply is used only when
  `0 < prompt_eval_count < num_ctx/2` and the reply fits in the context.
  Otherwise the tool retries once with half the byte budget, then exits 6.
- **Grounding.** The answer must be exactly `{step, errorLine, class}`, and
  it is checked against the **log's own lines**: the error-looking lines,
  the tail lines and the `== job / step ==` headers. The tool's own headings
  never count.
  - `step` must be at least 3 characters and appear in one of those lines.
  - The model's `errorLine` must be a verbatim quote of at least 8
    characters from **one** log line, not a header. The report then gives
    that **whole log line**, so a fragment quote still returns the complete
    message.
  - `class` must be in the policy.

  Anything else exits 6. A paraphrased, invented or trivially short quote is
  never passed on.

## Report (`projectbuddy/advise-triage@1`)

```json
{
  "schema": "projectbuddy/advise-triage@1", "tool": "0.1.0", "advisory": true,
  "verified": ["step", "errorLine"],
  "answer": { "step": "...", "errorLine": "...", "class": "segfault" },
  "model": { "name": "qwen2.5:7b", "digest": "..." }, "ollamaVersion": "...", "policySha256": "...",
  "input": { "bytes": 49106, "normalizedBytes": 25013, "sentBytes": 11999, "signalLines": 3, "droppedLines": 211, "attempts": 1 },
  "receipt": { "numCtx": 16384, "probePromptTokens": 8194, "promptTokens": 6827, "outputTokens": 143, "seconds": 7.5 }
}
```

`step` and `errorLine` are **verified extraction**. `class` is the model's
**advisory** diagnosis (#210 D11). For example, the spike behind #284 once
read a SIGSEGV as an out-of-memory kill.

On three real failed runs, using an 8 GB GPU with Ollama 0.32.3:
- each report was 685–961 bytes, against 46–65 KB of raw log (51–94× less);
- each took 3–8 s;
- every `errorLine` was a whole log line.

A PowerShell failure whose message spans several lines produced no advice
(exit 6) rather than a misquote.

Pre-review of diffs (`pre-review`) is the second slice of #284.
