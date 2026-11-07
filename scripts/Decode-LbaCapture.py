#!/usr/bin/env python3
"""Decode-LbaCapture.py: decode a `labview-benchmark-actor` VS Code
extension capture into a structured, human-readable summary for
agent/operator LabVIEW-VM troubleshooting.

Implements `ADR-0005` (`docs/architecture/adr/ADR-0005-agentic-labview-benchmark-actor-capture-decoding.md`,
corrected by its own Addendum): a dedicated, versioned, read-only decoder
for the extension's on-disk capture output (schema
`labview-benchmark-actor/launch-capture@1` + `.../capture-status@1`).
Diagnostic/operator-support tooling only -- never a CI gate, never
release-blocking.

This tool consumes `svelderrainruiz/labview-benchmark-actor`'s on-disk
output format only; it does not fork, vendor, or otherwise depend on that
project's own source. Captures land under VS Code's own per-extension
`globalStorage` directory as `run-<unix-ms-timestamp>` subdirectories, each
containing `capture-meta.json`, `capture-status.json`, `capture.json`, and
one `frame-NNNNN.png` per captured frame. There is no "latest capture"
pointer/index file anywhere in that storage -- this tool resolves the most
recently created `run-<timestamp>` directory itself when no explicit path
is given.

Usage:
    Decode-LbaCapture.py [run_dir] [--captures-root PATH] [--json OUT]

With no `run_dir`, the most recent `run-<timestamp>` directory under the
resolved captures root (an explicit `--captures-root`, or this tool's
OS-specific default guess at VS Code's `globalStorage` path) is used.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

LAUNCH_CAPTURE_SCHEMA = "labview-benchmark-actor/launch-capture@1"
CAPTURE_STATUS_SCHEMA = "labview-benchmark-actor/capture-status@1"
EXTENSION_STORAGE_ID = "svelderrainruiz.labview-benchmark-actor"

# The only state values the real extension ever writes (media/captureStatus.mjs's
# CAPTURE_STATES) -- 'stopped' is the *only* successful terminal state, not a
# sign of an incomplete/unsettled capture (see ADR-0005's Addendum).
_CAPTURE_STATUS_FAILED = "failed"
_CAPTURE_STATUS_CAPTURING = "capturing"
_CAPTURE_STATUS_STOPPED = "stopped"

_RUN_DIR_RE = re.compile(r"^run-(\d+)$")


def default_captures_root() -> Path:
    """Best-effort guess at VS Code's `globalStorage` captures directory for
    this extension, following VS Code's documented per-OS `globalStorage`
    convention (not independently re-verified on Windows/macOS hosts this
    tool has run on so far -- pass `--captures-root` explicitly if this
    guess is wrong for a given machine)."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "Code" / "User" / "globalStorage" / EXTENSION_STORAGE_ID / "captures"


def find_latest_run_dir(captures_root: Path) -> Path:
    """Resolves the most recently created `run-<unix-ms-timestamp>` directory
    under `captures_root`, since no upstream 'latest capture' pointer file
    exists. Sorts by the numeric timestamp encoded in the directory name
    itself (not filesystem mtime, which a copy/backup could change)."""
    if not captures_root.is_dir():
        raise FileNotFoundError(f"Captures root not found: {captures_root}")
    candidates = []
    for entry in captures_root.iterdir():
        match = _RUN_DIR_RE.match(entry.name)
        if entry.is_dir() and match:
            candidates.append((int(match.group(1)), entry))
    if not candidates:
        raise FileNotFoundError(f"No 'run-<timestamp>' capture directories found under {captures_root}")
    candidates.sort(key=lambda pair: pair[0])
    return candidates[-1][1]


def _load_json_checked(path: Path, expected_schema: str) -> tuple[dict[str, Any], str | None]:
    """Loads a JSON file and checks its own `schema` field against
    `expected_schema`. Returns `(data, warning)` -- an unrecognized/missing
    schema tag produces a non-None warning string (surfaced by the caller,
    never raised), the parse itself still proceeds best-effort rather than
    silently misinterpreting a schema the caller didn't pin for."""
    data = json.loads(path.read_text(encoding="utf-8"))
    actual = data.get("schema")
    warning = None
    if actual != expected_schema:
        warning = (
            f"{path.name}: schema is {actual!r}, expected {expected_schema!r} -- "
            "parsing best-effort; fields this tool relies on may be missing or "
            "shaped differently in a newer/older schema version"
        )
    return data, warning


def _min_max_avg(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    return {"min": min(values), "max": max(values), "avg": sum(values) / len(values)}


def summarize_capture(
    capture: dict[str, Any],
    status: dict[str, Any] | None,
    warnings: list[str],
) -> dict[str, Any]:
    """Builds the structured summary dict from an already-parsed `capture.json`
    (required) and `capture-status.json` (optional -- a capture-status beacon
    can be absent for an older/foreign capture). `warnings` accumulates
    schema-mismatch warnings found while loading; this function appends any
    further anomaly findings to its own returned `anomalies` list."""
    frames = capture.get("frames") or []
    anomalies: list[str] = list(warnings)

    cpu = _min_max_avg([f["cpuPct"] for f in frames if isinstance(f.get("cpuPct"), (int, float))])
    ram = _min_max_avg([f["ramMb"] for f in frames if isinstance(f.get("ramMb"), (int, float))])
    disk_pct = _min_max_avg([f["diskPct"] for f in frames if isinstance(f.get("diskPct"), (int, float))])

    per_disk: dict[str, dict[str, Any]] = {}
    peak_write = {"disk": None, "writeMBs": 0.0, "readMBs": 0.0, "frameIndex": None, "tMs": None}
    for frame in frames:
        for disk in frame.get("disks") or []:
            name = disk.get("name")
            if name is None:
                continue
            bucket = per_disk.setdefault(name, {"writeMBs": [], "readMBs": []})
            write_mbs = disk.get("writeMBs")
            read_mbs = disk.get("readMBs")
            if isinstance(write_mbs, (int, float)):
                bucket["writeMBs"].append(write_mbs)
                if write_mbs > peak_write["writeMBs"]:
                    peak_write = {
                        "disk": name,
                        "writeMBs": write_mbs,
                        "readMBs": read_mbs if isinstance(read_mbs, (int, float)) else 0.0,
                        "frameIndex": frame.get("index"),
                        "tMs": frame.get("tMs"),
                    }
            if isinstance(read_mbs, (int, float)):
                bucket["readMBs"].append(read_mbs)

    disks_summary = {
        name: {
            "writeMBs": _min_max_avg(bucket["writeMBs"]),
            "readMBs": _min_max_avg(bucket["readMBs"]),
        }
        for name, bucket in per_disk.items()
    }

    dual_packet = capture.get("dualPacket") or {}
    dual_packet_outcome = dual_packet.get("outcome")
    if dual_packet_outcome is not None and dual_packet_outcome != "authoritative":
        non_authoritative = [
            fr.get("frameIndex")
            for fr in (dual_packet.get("frames") or [])
            if fr.get("outcome") != "authoritative"
        ]
        anomalies.append(
            f"dualPacket.outcome is {dual_packet_outcome!r} (not 'authoritative'): "
            f"{len(non_authoritative)} of {dual_packet.get('frameCount')} frame(s) lost their "
            f"image payload to the mprr capacity policy -- first affected frame index(es): "
            f"{non_authoritative[:10]}"
        )

    # dhashHex: null on every frame is NORMAL for the real "Capture LabVIEW Launch"
    # command (it never computes a perceptual hash) -- only a MIXED population
    # within one capture is a genuine data-quality anomaly (ADR-0005 Addendum).
    dhash_populated = sum(1 for f in frames if isinstance(f.get("dhashHex"), str))
    if 0 < dhash_populated < len(frames):
        anomalies.append(
            f"dhashHex is populated on only {dhash_populated} of {len(frames)} frames -- "
            "inconsistent within one capture (normally either all-null or all-populated "
            "depending on which capture command produced it)"
        )

    if status is not None:
        state = status.get("state")
        if state == _CAPTURE_STATUS_FAILED:
            anomalies.append(f"capture-status.json reports state 'failed': {status.get('error')}")
        elif state == _CAPTURE_STATUS_CAPTURING:
            anomalies.append(
                "capture-status.json reports state 'capturing' -- this capture may never "
                "have finished (no stop beacon was ever written)"
            )
        elif state != _CAPTURE_STATUS_STOPPED:
            anomalies.append(
                f"capture-status.json reports an unrecognized state {state!r} "
                "(expected 'capturing'/'stopped'/'failed' -- possible schema drift)"
            )

    frame_count = capture.get("frameCount", len(frames))
    representative_frames = {}
    if frames:
        representative_frames["first"] = frames[0].get("image")
        representative_frames["last"] = frames[-1].get("image")
        if peak_write["frameIndex"] is not None:
            matching = [f for f in frames if f.get("index") == peak_write["frameIndex"]]
            if matching:
                representative_frames["peakIo"] = matching[0].get("image")

    return {
        "workload": capture.get("workload"),
        "plane": capture.get("plane"),
        "source": capture.get("source"),
        "fps": capture.get("fps"),
        "frameCount": frame_count,
        "durationMs": capture.get("durationMs"),
        "screen": capture.get("screen"),
        "resources": {
            "cpuPct": cpu,
            "ramMb": ram,
            "diskPct": disk_pct,
            "disks": disks_summary,
        },
        "peakIo": peak_write if peak_write["frameIndex"] is not None else None,
        "dualPacket": {
            "outcome": dual_packet_outcome,
            "authoritative": dual_packet.get("authoritative"),
            "authoritativeFrames": dual_packet.get("authoritativeFrames"),
            "frameCount": dual_packet.get("frameCount"),
        },
        "dhash": {"populatedFrames": dhash_populated, "totalFrames": len(frames)},
        "representativeFrames": representative_frames,
        "statusBeacon": status,
        "anomalies": anomalies,
    }


def cmd_summarize(run_dir: Path, json_path: Path | None) -> int:
    capture_path = run_dir / "capture.json"
    if not capture_path.is_file():
        print(f"capture.json not found in {run_dir}", file=sys.stderr)
        return 2

    warnings: list[str] = []
    capture, warning = _load_json_checked(capture_path, LAUNCH_CAPTURE_SCHEMA)
    if warning:
        warnings.append(warning)
        print(f"warning: {warning}", file=sys.stderr)

    status: dict[str, Any] | None = None
    status_path = run_dir / "capture-status.json"
    if status_path.is_file():
        status, status_warning = _load_json_checked(status_path, CAPTURE_STATUS_SCHEMA)
        if status_warning:
            warnings.append(status_warning)
            print(f"warning: {status_warning}", file=sys.stderr)

    summary = summarize_capture(capture, status, warnings)
    summary["runDir"] = str(run_dir)
    rendered = json.dumps(summary, indent=2)
    if json_path:
        json_path.write_text(rendered, encoding="utf-8")
        print(f"Wrote {json_path}")
    else:
        print(rendered)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "run_dir",
        type=Path,
        nargs="?",
        default=None,
        help="A specific 'run-<timestamp>' capture directory. Defaults to the most recent one under --captures-root.",
    )
    parser.add_argument(
        "--captures-root",
        type=Path,
        default=None,
        help="Override the captures root directory (default: this tool's OS-specific guess at VS Code's globalStorage path).",
    )
    parser.add_argument("--json", type=Path, default=None, dest="json_path", help="Write the summary to this file instead of stdout.")
    args = parser.parse_args()

    run_dir = args.run_dir
    if run_dir is None:
        captures_root = args.captures_root or default_captures_root()
        try:
            run_dir = find_latest_run_dir(captures_root)
        except FileNotFoundError as err:
            print(str(err), file=sys.stderr)
            return 2
    elif not run_dir.is_dir():
        print(f"run_dir not found: {run_dir}", file=sys.stderr)
        return 2

    return cmd_summarize(run_dir, args.json_path)


if __name__ == "__main__":
    raise SystemExit(main())
