# `bench/`: `projectbuddy-bench`

`projectbuddy-bench` picks the base model for the `projectbuddy` identity
(stage P1, [#217](https://github.com/VITechnologies/projectbuddy/issues/217)).
It scores candidate models against D10, "Base model", in
[#210](https://github.com/VITechnologies/projectbuddy/issues/210). It is a
self-contained .NET 10 single-file program, so the same binary logic runs on
the Linux workstation (`linux-x64`) and on the Windows peer (`win-x64`).

```bash
dotnet publish ProjectBuddy.PeerBench -c Release -r linux-x64 -o out
out/projectbuddy-bench run --corpus corpus.json --candidates candidates.json \
  --machine workstation --ollama http://127.0.0.1:11434 > result.json
```

- `--machine` is a role, `workstation` or `peer`. Hostnames and addresses
  never enter the result.
- `--ollama` must be `http://` plus a loopback IP literal and a port.
  `localhost`, other addresses, paths, queries and credentials are refused,
  because Ollama stays bound to loopback (D2).
- `--timeout-seconds` bounds each request (default 600, maximum 3600).
- The result JSON goes to stdout and one progress line per item goes to stderr.
- Exit codes: 2 usage, 3 refused input, 4 the truncation probe failed, and 5
  Ollama was unreachable or answered with an invalid response.

## Inputs

- **`candidates.json`** (`projectbuddy/peer-bench-candidates@1`) pins each
  candidate to its full Ollama manifest digest. If any candidate is missing or
  has a different digest, the run is refused.
- **`corpus.json`** (`projectbuddy/peer-bench-corpus@1`) fixes these values:
  - `num_ctx`, `num_predict` and the seed; temperature is always 0;
  - one system prompt per kind;
  - the allowed classes and verbs;
  - the items.

  Every item records its `source`. Real items cite a **public** run and are
  trimmed excerpts with the timestamps stripped. Synthetic items use generic
  names only. Each item is capped at 8 KiB. A triage item's ground-truth
  `errorLine` must appear verbatim in its input.

| Kind | What the model returns | Scored as (D10) |
|---|---|---|
| `triage` | JSON `{step, errorLine, class}` | **Extraction**: the right step, and an error line copied verbatim from the input. **Diagnosis**: the right class. |
| `security` | JSON `{findings: [{class, line}]}` | **Security recall**: expected classes found. **False positives**: classes reported but not expected, including on the clean controls. |
| `tool` | A native Ollama `tool_calls` entry for `ask_peer(verb)` | **Tool calling**: exactly one call with the right verb. JSON written into the reply text does not count. |

Model output is scored and never trusted. A reply with the wrong shape counts
as `malformed`, and the raw text is not kept in the result.

## Truncation guard

Ollama does not report prompt truncation. When a prompt is longer than
`num_ctx`, it keeps only the last `num_ctx/2 + 2` tokens, and the system prompt
is dropped with them. For example, `num_ctx` 8192 gives `prompt_eval_count`
4098. The harness therefore:

1. **Probes each model first.** It sends a prompt of about `2 × num_ctx` words
   and requires the reported count to be at least `num_ctx/2`. Otherwise it
   exits 4: the server no longer behaves as the guard assumes, so no score can
   be trusted.
2. **Accepts an item's reply only if it provably fit.** That means
   `0 < prompt_eval_count < num_ctx/2` and
   `prompt_eval_count + eval_count ≤ num_ctx`. Any other reply is counted as
   `rejected`. A valid result has zero rejected items.

The probe, the accept rule, the loopback-only client and the digest pins
live in the shared [`../ollama`](../ollama/README.md) library, so
`projectbuddy-advise` enforces exactly the same guards.

## Result

The result (`projectbuddy/peer-bench-result@1`) records the Ollama version,
the corpus SHA-256, and the following for each model:

- the probe count;
- VRAM residency from `/api/ps`, where `fullGpu` means the whole model is in
  VRAM;
- the triage, security and tool tallies;
- the `malformed` and `rejected` counts;
- the median latency and mean output rate;
- one outcome per item.

Each model is unloaded after its run. Results are evidence for the D10 choice.
They are kept in the operator's private state log, not committed here.

## Choosing the base (D10)

1. **Hard requirements:**
   - `fullGpu` on both machines;
   - zero `rejected` and zero `malformed`;
   - native tool calls correct on at least 80% of the tool items, because the
     P4 agent depends on tool calling.
2. **Then rank by, in order:**
   - **security F1** (`2·hits / (2·hits + falsePositives + misses)`). Recall
     alone rewards a model that flags everything, clean controls included.
   - triage extraction, then diagnosis;
   - median latency.

   Rank on totals **pooled across both machines**. At temperature 0 with a
   fixed seed, scores still differ between machines and Ollama versions; in
   #217, `llama3.1:8b` extraction was 5/8 on one machine and 2/8 on the
   other. Treat F1 differences under 0.05 as ties and move to the next
   criterion.

Record the choice and both machines' results on #217 before generating the
identity Modelfile for the chosen base. The first choice was `qwen2.5:7b`,
giving the identity `projectbuddy:<identityVersion>-qwen2.5-7b`: first
`projectbuddy:0.1-qwen2.5-7b`, now `projectbuddy:0.3-qwen2.5-7b` (#358).
