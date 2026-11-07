# ADR-0005: Agentic Decoding of `labview-benchmark-actor` Capture Output

- Status: Accepted
- Date: 2026-11-07
- Deciders: svelderrainruiz (operator), decided directly by the agent-operator
  under this session's standing operator authorization, grounded in this
  session's own real use of the tool (capture `run-1794072448721`)

## Context

`labview-benchmark-actor` (`svelderrainruiz/labview-benchmark-actor`, a
separate, public, already-functional project with its own VS Code
extension) captures detailed, cross-platform (Windows/Linux) LabVIEW
launch/runtime telemetry on demand: per-frame CPU/RAM/disk-I/O samples
plus a screenshot per frame, written to disk under
`~/.config/Code/User/globalStorage/svelderrainruiz.labview-benchmark-actor/captures/run-<unix-ms-timestamp>/`
(Linux; the equivalent VS Code `globalStorage` path convention applies on
Windows, not independently re-verified on a Windows host this session) as
`capture-meta.json` (workload/plane/source), `capture-status.json`
(schema `labview-benchmark-actor/capture-status@1`: run state, timing,
frame count, peak per-disk I/O), `capture.json` (schema
`labview-benchmark-actor/launch-capture@1`: the full per-frame series plus
a `dualPacket` frame-integrity structure), and one `frame-NNNNN.png` per
captured frame.

This engine (`projectbuddy`) is already deeply invested in LabVIEW-VM
troubleshooting on both platforms (`vm/windows-labview/`,
`vm/ubuntu-labview/`, the planned LUnit gate's own Windows/Linux
diagnostics). An agent-operator supporting a human operator on this engine
benefits directly from being able to read a `labview-benchmark-actor`
capture a human operator hands it, the same way it already reads a JUnit
report or a VirtualBox log -- but today has no first-class, tested way to
do so.

This session used the tool for exactly that purpose and found genuine
signal in a real capture (`run-1794072448721`: 52.5s, 631 frames, Linux
plane, `gnome-shell-screencast` source), confirming the approach has real
value, but only by direct, ad-hoc, repeated parsing of the raw JSON --
with real mistakes along the way worth recording so the next agent does
not repeat them:

- The extension's own MCP viewer tools (`lbaBenchmarkSummary`,
  `lbaBenchmarkPanel`) showed fixed demo numbers (2577ms, 481 frames)
  before and after this real capture -- they do not reflect a newly
  recorded run. There is no "latest capture" index/pointer file anywhere
  in the extension's storage; an agent must independently resolve the
  correct `run-<timestamp>` directory (by directory name or
  `capture-meta.json`), and must read the raw `capture.json`/
  `capture-status.json` files directly to see a specific real capture's
  actual data.
- `capture.json`'s `resources.{cpu,ram,disk}` arrays are flat parallel
  numeric series (one float per sample) -- an ad-hoc assumption that
  `resources.disk` held per-disk nested objects was wrong and raised an
  `AttributeError` before the real, richer structure was found: the
  top-level `frames[]` array, where each entry already carries
  `cpuPct`/`ramMb`/`diskPct` plus a `disks[]` breakdown
  (`name`/`writeMBs`/`readMBs`) and the matching `image`/`imageBytes`
  filename for that same instant.
- All 631 frames had `dhashHex: null` and the capture's own top-level
  `screen` field was `null` -- the extension's perceptual-hash-based
  "settle fingerprint" mechanism (visible as real, non-null data in the
  fixed MCP demo output) produced no value at all for this genuine,
  manually-stopped capture. Reading `capture-status.json`'s
  `"state": "stopped"` (vs. a natural completion state) is the only
  direct, schema-backed signal available to flag "this capture may not
  have reached a settled end state" -- `screen: null` is at most a
  corroborating, not an independently confirmed, signal for the same
  thing.
- Visually sampling a few PNG frames directly (via a general image-viewing
  tool, frame 1 and frame 300 of the 631) found the captured screen area
  was the operator's entire desktop (browser, VS Code, this very
  conversation) with no LabVIEW window visible in either sampled frame,
  despite `cpuPct` staying substantially non-zero (oscillating roughly
  10-70%) throughout. Per-disk data shows a single large read burst at
  frame 0 only (252.456 MB/s on `nvme0n1`, decaying to near-zero within
  about 2 seconds) and negligible disk activity for the rest of the
  52.5s window -- consistent with a one-time executable/library
  cold-start read, with the sustained CPU activity's cause not resolved
  from the numeric/metadata series alone.

None of this was captured anywhere reusable; a future agent session facing
the same capture (or any other) would redo all of the above from scratch,
including the same wrong assumption about `resources.disk`'s shape.

## Decision

`projectbuddy` adopts a dedicated, versioned, read-only decoding
capability for `labview-benchmark-actor` capture output, for agent use
during troubleshooting sessions -- diagnostic/operator-support tooling,
the same category as `vm-fleet-status.yml`, **not** a CI gate and not
release-blocking.

- **Location and form**: `scripts/Decode-LbaCapture.py`, a standalone
  Python script (no LabVIEW/.NET dependency -- the data is plain JSON +
  PNG), matching this repo's existing Python-script convention
  (`scripts/Convert-VipbJson.py`).
- **Discovery**: resolves the extension's `globalStorage/captures/` root
  and, absent an explicit path argument, selects the most recent
  `run-<timestamp>` directory itself (by directory name), since no
  upstream "latest capture" pointer exists.
- **Schema handling**: reads `capture.json`'s and `capture-status.json`'s
  own `schema` field and checks it against the pinned tags this ADR
  records (`labview-benchmark-actor/launch-capture@1`,
  `labview-benchmark-actor/capture-status@1`). An unrecognized/newer
  schema tag produces a visible warning and a best-effort parse, never a
  silent misinterpretation -- the same "pin and bump deliberately"
  discipline this engine already applies to every other external
  dependency (`README.md`'s Consumption model), applied here to a data
  schema contract instead of a code artifact, since this tool consumes
  the extension's on-disk output format only and does not fork or vendor
  the extension's own source.
- **Output**: a structured summary covering duration, frame count,
  per-resource (CPU/RAM/disk, per-disk-name) min/max/avg, the peak I/O
  event(s) with frame index and `tMs`, the `dualPacket.outcome`/
  `driftClass` integrity result if present, and explicit anomaly flags for
  the specific conditions this ADR's own motivating analysis found worth
  flagging: `dhashHex` null across every frame, `capture-status.json`'s
  `state` not indicating a natural completion, and a short, explicit
  pointer to specific frame indices (first, peak-I/O, last) worth a
  follow-up manual look -- not an automatic verdict about them.
- **Explicit v1 non-goal**: automatic visual/image-based analysis of the
  PNG frames (e.g. detecting "no LabVIEW window visible" without a human
  or agent looking at the image). This session's own real finding above
  was reached by a general-purpose agent image-viewing tool on a handful
  of chosen frames, not by any bespoke image classifier -- v1 of this
  tool only narrows down *which* frames are worth that same manual look,
  consistent with this project's established preference (`#98`) for not
  building more tool than the current, concrete need justifies.

Implementation (the script itself, its tests, and its doc placement) is
tracked as a follow-up, not performed by this ADR: `#102`.

## Consequences

- Future agent sessions troubleshooting LabVIEW-VM issues on either
  platform gain a reusable, tested way to pull signal out of a human
  operator's `labview-benchmark-actor` capture, instead of re-deriving
  ad-hoc parsing (and repeating this session's real `resources.disk`
  shape mistake) from scratch each time.
- Introduces a new explicit external-schema dependency this engine does
  not control (`labview-benchmark-actor`'s capture JSON schema); a schema
  version bump upstream must be noticed (via the warn-on-mismatch
  behavior above), not silently misparsed.
- Purely a local-filesystem, read-only consumer of artifacts the
  extension already produces -- no new runtime/network dependency, and no
  change to any existing CI/release gate.
- Wiki `REQ-037` (Planned) and `Requirements-Traceability-Matrix.md`
  record this decision's requirement; `REQ-037` promotes to Active once
  `#102` lands with a real verification run against a genuine capture
  (not only a synthetic fixture), matching this project's existing
  Planned -> Active promotion convention (`REQ-033`).

## Related

- `#102` (implementation follow-up)
- `svelderrainruiz/labview-benchmark-actor` (the external project whose
  output this ADR's tooling decodes; not modified or vendored by this
  decision)
- This session's real capture, `run-1794072448721` (Linux plane,
  `gnome-shell-screencast` source, 52.5s/631 frames/375 resource samples),
  the motivating evidence above
- Wiki SRS `REQ-037`
- `#98` / `docs/lunit-gate/adapting-for-a-new-project.md` (the precedent
  for preferring a narrow, concrete tool over a speculative general one)
