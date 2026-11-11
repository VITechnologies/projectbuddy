# Assisted benchmark contract

`lba-mcp-contract.json` pins the ProjectBuddy-facing LBA MCP boundary to the
actor protocol and tool schemas verified from
`svelderrainruiz/labview-benchmark-actor` revision
`7f65a2215b952e1f31bc3677087f14c8b528d789` (package 1.4.14), protocol
`2025-06-18`.
The four core tools are required. The optional ACG grid tools must be either
all present with the pinned schemas or all absent; an unknown or partial set
fails closed.

`scripts/assisted_benchmark_contract.py` validates a normalized MCP snapshot
and per-run authorization records. Each approval binds exact tool names and
arguments, the actor revision, local Ollama tag/digest, evidence fields, and
target VM UUIDs. Unknown tools, partial optional groups, schema drift, and
unapproved calls fail closed. `plan-only` tools return commands for review;
ProjectBuddy must never execute command text returned by a tool.

Example snapshot shape, derived from MCP `initialize` and `tools/list` results:

```json
{
  "protocolVersion": "2025-06-18",
  "serverInfo": {"name": "labview-benchmark-actor", "version": "1.4.14"},
  "tools": []
}
```

Validate an exported snapshot with:

```bash
python3 scripts/assisted_benchmark_contract.py validate-snapshot /path/to/snapshot.json
```

The snapshot must contain the actual `tools/list` array; the empty array above
is only a shape illustration and will be rejected because required tools are
missing.

## Capture and review

The capture reader accepts only a finalized `stopped` run with a ready
`capture.json`. It verifies run identity, status/manifest counts, resource JSONL,
relative frame names, PNG headers/dimensions, and each frame's byte count. It
computes SHA-256 per frame and a deterministic identity hash without copying the
images or video into the evidence bundle:

```bash
python3 scripts/assisted_benchmark_capture.py <capture-run-directory> \
  --actor-version 1.4.14 \
  --actor-revision 7f65a2215b952e1f31bc3677087f14c8b528d789
```

The run coordinator starts the MCP command without a shell, performs the
initialize/tools-list handshake, validates the pinned surface, and calls only
the exact per-run-approved MCP calls. Tool results are hashed; their raw
payloads are not placed in the receipt. It then verifies the `lv_icon.lvlibp`
benchmark receipt and obtains an Ollama assessment:

```bash
umask 077
python3 scripts/assisted_benchmark_run.py \
  --authorization <protected-run-authorization.json> \
  --capture-directory <capture-run-directory> \
  --ppl-proof <verified-lv-icon-ppl-receipt.json> \
  --contract tools/assisted-benchmark/lba-mcp-contract.json \
  --ollama-url http://127.0.0.1:11434 \
  --output "${XDG_RUNTIME_DIR:?}/assisted-benchmark/<run-id>.json" \
  --mcp-command node <actor-extension>/out/mcp/runBenchmarkActorMcpServer.js
```

The authorization must use schema `projectbuddy/assisted-benchmark-authorization@1`.
It names exact approved calls and JSON arguments, the full actor source revision,
Ollama model tag/digest, evidence fields, and lifecycle/cleanup authority. The
receipt writer requires a new output file under a private POSIX directory,
writes atomically with mode `0600`, and prints only completion and receipt hash.

For a read-only capture assessment, an authorization has this shape (replace
the example run/model values with the actual stopped run and locally verified
model tag/digest):

```json
{
  "schema": "projectbuddy/assisted-benchmark-authorization@1",
  "runId": "run-<capture-folder-id>",
  "workload": "labview-launch",
  "sourceRevision": "7f65a2215b952e1f31bc3677087f14c8b528d789",
  "approvedBy": "operator",
  "approvedAt": "<ISO-8601 timestamp with timezone>",
  "approvedCalls": [
    {"tool": "get_host_capabilities", "arguments": {}},
    {"tool": "get_benchmark_series", "arguments": {}}
  ],
  "targetVmUuids": [],
  "ollamaModel": {"tag": "projectbuddy:...", "digest": "<64 hex characters>"},
  "reviewBudget": {
    "schema": "projectbuddy/ollama-budget@1",
    "accounting": "utf8-byte-plus-approved-media@1",
    "modelDigest": "<same full lowercase model digest>",
    "contextTokens": 16384,
    "outputTokens": 512,
    "templateReserveTokens": 1024,
    "imageTokensPerImage": 0,
    "maxTextBytes": 65536,
    "maxImageBytes": 0,
    "maxImagesPerRequest": 0
  },
  "reviewPrivacy": {
    "schema": "projectbuddy/ollama-privacy@1",
    "imageMode": "none",
    "frames": [],
    "retainModelText": false,
    "expiresAt": "<future ISO-8601 timestamp with timezone>"
  },
  "evidenceFields": ["captureMetadata", "frameHashes", "resourceSamples", "benchmarkResult"],
  "allowVmLifecycle": false,
  "teardownOnNativePplFailure": false
}
```

`approvedCalls` is an exact tool-plus-arguments allow-list, not a set of tool
names. The current actor MCP does not expose capture discovery/read tools, so
the run directory remains an explicit local input until the actor-side MCP
capture contract tracked by `svelderrainruiz/labview-benchmark-actor#44` is
implemented.

When `frameImages` is approved, the reviewer samples at most the first, middle,
and last frame, verifies each against the recorded SHA-256, and sends one image
per request to a local Ollama model advertising the `vision` capability. Raw
frame/video bytes are not sent to remote services or included in the receipt.
The `projectbuddy:0.1-qwen2.5vl-3b` identity is vision-capable; its tag and
digest must still match the per-run authorization.

The approved tag and full lowercase SHA-256 digest are required inputs, never
adopted automatically from inventory discovery. Missing, malformed, duplicate,
or conflicting inventory entries block inference; a postflight identity error
blocks receipt completion. Assessment and run receipts retain approved model
identity separately from the verified review identity (`approvedModel` / `model`
and `approvedReviewModel` / `reviewModel`). This is the `REQ-109` / `TEST-111`
contract in ProjectBuddy issue #230, not attestation of an untrusted local daemon.

## Aggregation

The host validates every required per-input response and revalidates the final
aggregate. A failing input remains failing; only all-pass inputs without any
uncertainties can produce a passing aggregate. Other complete combinations are
inconclusive. Missing, malformed, or contradictory inputs are errors, not passes.

Findings are deduplicated by their complete validated content, with evidence
field order normalized. Different supporting evidence is never collapsed merely
because claim text matches. Ordering is stable: blocking, warning, then info;
within a severity, first occurrence in input order wins. The 32-finding output
bound never drops a blocker: more than 32 distinct blockers causes an incomplete
review error. Optional detail may be bounded, but omission counts are disclosed
in `aggregation` on assessment receipts and `reviewAggregation` on run receipts,
with policy version and required/completed input counts. Uncertainties are
deduplicated and bounded to 32; multiple distinct recommendations use an explicit
combined-review recommendation and report how many original recommendations
were omitted. This is the `REQ-110` / `TEST-112` contract for issue #231.
Frame-specific finding references remain separate work in issue #233; category
references are not presented as frame-level attribution or whole-capture proof.

## Inference Budget

Model review requires an explicit `reviewBudget` bound into run authorization;
authorization used only for planning MCP calls does not implicitly grant one.
The example above is text-only. Image review additionally requires a reviewed,
digest-specific `imageTokensPerImage` upper bound, nonzero `maxImageBytes`, and
`maxImagesPerRequest: 1`; unknown image cost means no image inference. Fixture
image costs are synthetic, not a live-model sizing recommendation.

Only the declared `utf8-byte-plus-approved-media@1` accounting for a model
reporting the `gpt2` byte-level tokenizer is supported. Model metadata must
provide its architecture/context limit and template/system. Template/system
UTF-8 bytes plus 256 framing tokens must fit the approved reserve. The host
counts prompt/schema UTF-8 bytes conservatively, adds that reserve and the
approved media bound, and reserves output tokens before any request is sent.
All requests are prechecked; nothing is silently truncated or summarized.
Completion counts must be positive and within bounds, in addition to existing
identity, JSON-schema, evidence, and completion checks. Unsupported metadata or
an input that cannot be shown to fit is rejected, not submitted optimistically.

Assessment and run receipts retain the complete approved budget, per-request
policy/request hashes, input upper bounds, and no-truncation coverage, plus the
observed tokenizer/context and template hash. They do not retain raw prompts or
image data in budget metadata. These checks trust local daemon metadata and the
caller-reviewed template/media bounds; they do not prove model reasoning,
authenticate weights, or establish GPU capacity. The policy implements the
`REQ-111` / `TEST-113` contract for issue #232.

## Disclosure And Retention

Model review requires an explicit expiring `reviewPrivacy` policy in addition
to approved evidence fields and budget. `imageMode: none` permits no images;
`full-frame` explicitly grants the host-selected complete frames; `regions`
requires one policy entry for each selected frame. Model output cannot change
that policy. A region entry has this shape:

```json
{"index": 0, "crop": [0, 0, 600, 400], "redactions": [[10, 10, 100, 30]]}
```

Boxes are integer `[left, top, width, height]` coordinates in the original
image, with positive sizes and no out-of-bounds area. Redactions are solid
black and applied before cropping. Pillow 12.1.1 performs bounded PNG decoding,
alpha compositing onto black, redaction/crop, and deterministic metadata-free
PNG encoding without modifying captures. Install the pinned dependency with
`python -m pip install Pillow==12.1.1` for image review; absent support refuses
image inference. Source and submitted-image hashes plus policy hashes bind
exact disclosure. Unknown or missing regions do not fall back to a desktop.

`retainModelText` currently supports only `false`. Returned model prose is
replaced by fixed host summaries; original validated assessment/finding hashes
retain audit identity, not text. Expiry is rechecked before requests and final
retention; it blocks reuse and reports explicit cleanup needs without deleting
operator assets. Receipts stay private; entry-point errors do not echo private
paths or raw model output. Synthetic canary tests verify these defined pixel,
metadata, text, and failure paths, not detection of every unknown secret.
This is the `REQ-113` / `TEST-115` contract for issue #234.

This prototype does **not** start, stop, create, delete, activate, or register a
VM, and does not execute the PPL build; it validates an existing
`lv_icon.lvlibp` proof. Native build failures and exact-VM poison teardown remain
behind the separately approved lifecycle workflow. Do not treat an
`inconclusive` model assessment as a passing benchmark.