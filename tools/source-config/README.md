# Source Configuration Prototype

The .NET 10 CLI generates a pipeline JSON, VIPM TOML, and evidence report from
an existing source manifest and an explicitly selected packed-library build.
It is a candidate implementation, not a released onboarding command or proof
that the generated configuration builds successfully on Windows.

## Requirements

- The pinned SDK in `global.json`.
- Git and an existing checkout at an explicit 40- or 64-character commit ID.
- A tracked VIPM TOML declaring project name, LabVIEW year and bitness, exact
  VIPM dependency versions, and a standalone `lvproj` build target.
- A tracked LabVIEW XML project containing that packed-library specification,
  an explicit SBOM JSON path, and an explicit artifact directory.
- A running local Ollama server with the selected model already installed,
  plus its caller-approved model tag and full lowercase SHA-256 digest.
  There is no cloud or no-model fallback.
- The engine's `pipeline-config.schema.json` and a new output directory outside
  the inspected checkout. Its parent directory must already exist.

Metadata files must match their Git blobs byte for byte. For Windows checkouts,
disable automatic line-ending conversion when cloning this evidence checkout;
otherwise converted metadata is rejected rather than silently treated as the
pinned bytes. Symlinks and reparse points in inspected paths are refused.

## Run

From `tools/source-config`, replace the uppercase values with the selected
repository, commit, paths, build key, and installed model:

```bash
dotnet restore ProjectBuddy.SourceConfig --locked-mode
dotnet run --no-restore --project ProjectBuddy.SourceConfig -- \
  --root /path/to/CHECKOUT \
  --manifest vipm.toml \
  --build BUILD_KEY \
  --repository https://github.com/OWNER/REPOSITORY.git \
  --revision COMMIT_ID \
  --schema ../../pipeline-config.schema.json \
  --output /path/to/NEW_OUTPUT_DIRECTORY \
  --ollama-url http://127.0.0.1:11435 \
  --model MODEL:TAG \
  --model-digest APPROVED_MODEL_SHA256 \
  --review-budget /path/to/APPROVED_BUDGET.json \
  --review-privacy /path/to/APPROVED_PRIVACY.json \
  --review-lifecycle /path/to/APPROVED_LIFECYCLE.json \
  --review-execution /path/to/APPROVED_EXECUTION.json
```

The same options work with native Windows paths in PowerShell; supply the
arguments on one line or use PowerShell continuation syntax, not Bash slashes.
Use the port on which the operator's Ollama server actually listens. The CLI
accepts only literal IPv4/IPv6 loopback HTTP(S) origins and refuses redirects.
It does not change GPU settings or start, install, or download a model.

`--model-digest` is required approval input, not a value selected automatically
from the server. Inventory discovery alone does not grant approval. Before any
review prompt is sent, the selected tag must resolve uniquely to that exact
digest; malformed/ambiguous inventory or a different installed digest blocks
inference. The identity is checked again after review. Missing or changed
postflight identity leaves no generated configuration or success evidence.
The local daemon remains a trusted dependency; these checks are not a signed
attestation of the model weights or protection against a malicious daemon.

`--review-budget` is a required caller-approved JSON policy. It has exactly
these fields (replace the digest with the approved model's full digest):

```json
{
  "schema": "projectbuddy/ollama-budget@1",
  "accounting": "utf8-byte-plus-approved-media@1",
  "modelDigest": "<64 lowercase hex characters>",
  "contextTokens": 16384,
  "outputTokens": 512,
  "templateReserveTokens": 1024,
  "imageTokensPerImage": 0,
  "maxTextBytes": 65536,
  "maxImageBytes": 0,
  "maxImagesPerRequest": 0
}
```

The engine supports this conservative accounting only for a model reporting
the `gpt2` byte-level tokenizer. Model metadata must expose a sufficient context
length and a template/system whose UTF-8 bytes plus a 256-token framing reserve
fit the approved template reserve. Prompt roles/content, output schema, template
reserve, and output-token reserve must all fit; a byte allowance alone is not
acceptance. Unknown metadata, an unsupported tokenizer, or excess content is
refused without inference. Nothing is silently truncated or automatically
summarized. Completion counts must be present, positive, and within the approved
bounds; incomplete/length-limited responses cannot pass. This compatibility
contract trusts local daemon metadata and the caller's reviewed policy, not a
model attestation or universal tokenizer guarantee. Source-config accepts no
images. These example limits are not benchmark or GPU-capacity proof.

## Outputs And Gates

`--review-execution` is required. Only explicitly approved CPU-only execution
is currently supported; replace the digest with the same full lowercase digest
as `--model-digest`:

```json
{
  "schema": "projectbuddy/ollama-execution@1",
  "modelDigest": "<64 lowercase hex characters>",
  "mode": "cpu-only",
  "threads": 1
}
```

The host validates exactly these fields and an integer thread count from 1 to 8.
It emits `num_gpu: 0` and the approved `num_thread` in the canonical payload
before request hashing and submission. Unknown modes/controls or a different
digest fail before inference; no server-default or after-hashing adapter fallback
is used. The policy is retained as `reviewExecution` in evidence. These are
requested Ollama execution settings, not proof of OS CPU/GPU enforcement or
exclusive service ownership; observed residency remains a separate fact.

`--review-lifecycle` is required. This example bounds the model-review session,
including model preflight, inference, backoff and postflight (not earlier Git
source discovery or a separate build):

```json
{
  "schema": "projectbuddy/ollama-lifecycle@1",
  "totalSeconds": 300,
  "requestSeconds": 90,
  "maxRequests": 16,
  "maxRetries": 0,
  "backoffSeconds": 0,
  "residencySeconds": 30,
  "maxImageCount": 0
}
```

Linked cancellation tokens bound headers and body reads as well as overall
review time. Request counts include metadata/postflight and retry attempts;
running out cannot skip identity checks. Only declared transient HTTP/transport
failures may retry with the same approved payload. Invalid schema/identity,
privacy failure or timeout does not fall back to weaker settings. Ctrl+C cancels
the client path and cannot produce valid configuration. A timeout/cancellation
does not prove server computation stopped; diagnostics state that uncertainty.
Requested residency is a finite TTL, never an unowned forced unload. The engine
does not stop/reconfigure Ollama or delete models. Completion evidence records
the policy, counts, consumed time and outcome. No CPU/GPU hard-limit enforcement
is implied; unsupported extra policy fields are rejected.

`--review-privacy` is required and uses this text-only policy (set an explicit,
future timezone-bearing expiry for the authorized run):

```json
{
  "schema": "projectbuddy/ollama-privacy@1",
  "imageMode": "none",
  "frames": [],
  "retainModelText": false,
  "expiresAt": "<future ISO-8601 timestamp with timezone>"
}
```

The engine never saves arbitrary model prose. Retained blockers are fixed
host-authored summaries; a hash binds the original validated model result.
Expired policies block inference/reuse, including postflight, without deleting
operator-owned files. Entry-point errors identify their class without echoing
private paths or untrusted model text. Generated outputs use a 0700 directory
and 0600 files on Linux, or exact current-user-plus-SYSTEM access on Windows
(the SID union is one identity when the current user is SYSTEM). Unsupported
or unsafe custody fails closed. These controls are minimization, not a claim
that a classifier or regex recognizes every possible secret.

- `pipeline.json`: schema-validated native Windows route, pinned source commit,
  and source-derived artifact metadata, with automatic cleanup disabled. For a
  PPL, `artifactPath` is the file named by matching
  `Destination[0].destName` and `TgtF_targetfileName` under the
  `Bld_localDestDir`; the containing directory alone is not a valid artifact
  path.
- `vipm.toml`: preserved project/dependency values and only the selected build.
  This slice does not synthesize missing manifests or resolve binary-VI imports.
- `evidence.json`: input and output SHA-256 values, source revision, field
  provenance, schema hash, caller approval (`approvedOllama`), verified model
  identity (`ollama`), decision, blockers, and validity flags.
  The report also retains the approved budget and request/policy/template hashes,
  conservative input bound, and explicit no-truncation coverage without saving
  the raw request in the budget record.

Exit 0 means deterministic checks and the mandatory local model review passed
for this declared configuration scope. Exit 2 means drafts were produced but
the model rejected or could not approve them. Exit 1 means an input, transport,
review format, or other validation failed; do not consume partial output.
Existing output directories are never reused. Neither successful generation
nor model acceptance is native-build proof: `nativeBuildProven` remains false.

The model reviews configuration consistency, not toolchain installation,
signature availability, or completeness of dependencies hidden in binary VIs.
A model's acceptance cannot override schema or source-evidence failures.
Requests have explicit finite review/request deadlines, bounded response size and token count,
and no tool-execution channel. Source-derived prompt values remain untrusted.
Keep generated evidence private when the inspected source or its paths are
private. No configuration is registered, no workflow dispatched, no source
command executed, and no VM, model, image, or volume is cleaned up.

## Tests

```bash
dotnet restore ProjectBuddy.SourceConfig.Tests --locked-mode
dotnet test ProjectBuddy.SourceConfig.Tests --no-restore
```

Tests use synthetic Git repositories, mocked review results, and fake HTTP
handlers covering zero-inference preflight rejection and postflight identity
failure. They do not
require Ollama, LabVIEW, GPU access, or a registered runner. Linux and Windows
CI jobs run the same suite; actual CI results and a native LabVIEW build remain
separate gates. The planned contract is the wiki's `REQ-108` and `TEST-110`.
Caller-approved model identity is specified in `REQ-109` and `TEST-111`
(ProjectBuddy issue #230).
Budget/completeness behavior is specified in `REQ-111` and `TEST-113`
(ProjectBuddy issue #232).
Disclosure/retention behavior is specified in `REQ-113` and `TEST-115`
(ProjectBuddy issue #234).
Review lifecycle behavior is specified in `REQ-114` and `TEST-116`
(ProjectBuddy issue #235).
Explicit execution options are specified in `REQ-118` and `TEST-119`
(ProjectBuddy issue #244).