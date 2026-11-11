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
  "evidenceFields": ["captureMetadata", "frameHashes", "resourceSamples", "frameImages", "benchmarkResult"],
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

This prototype does **not** start, stop, create, delete, activate, or register a
VM, and does not execute the PPL build; it validates an existing
`lv_icon.lvlibp` proof. Native build failures and exact-VM poison teardown remain
behind the separately approved lifecycle workflow. Do not treat an
`inconclusive` model assessment as a passing benchmark.