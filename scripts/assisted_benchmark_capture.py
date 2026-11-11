#!/usr/bin/env python3
"""Validate a stopped LBA capture and emit local, redacted evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import struct
import sys
from datetime import datetime
from pathlib import Path

PNG_SIGNATURE = bytes.fromhex("89504e470d0a1a0a")
CAPTURE_SCHEMA = "labview-benchmark-actor/launch-capture@1"
STATUS_SCHEMA = "labview-benchmark-actor/capture-status@1"
EVIDENCE_SCHEMA = "projectbuddy/assisted-benchmark-evidence@1"
FRAME_NAME = re.compile(r"^frame-[0-9]{5,}\.png$")
RUN_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
ACTOR_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
GIT_REVISION = re.compile(r"^[0-9a-f]{40}$")
MAX_IMAGE_BYTES = 128 * 1024 * 1024


class CaptureError(ValueError):
    pass


def read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CaptureError("required capture JSON is unreadable or malformed") from error


def finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise CaptureError(f"{label} must be a finite number")
    return float(value)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def png_dimensions(path: Path) -> tuple[int, int]:
    if path.is_symlink() or not path.is_file():
        raise CaptureError("frame image is missing or is a symlink")
    size = path.stat().st_size
    if size < 24 or size > MAX_IMAGE_BYTES:
        raise CaptureError("frame image size is outside accepted bounds")
    with path.open("rb") as stream:
        header = stream.read(24)
    if header[:8] != PNG_SIGNATURE or struct.unpack(">I", header[8:12])[0] != 13 or header[12:16] != b"IHDR":
        raise CaptureError("frame image is not a valid PNG")
    width, height = struct.unpack(">II", header[16:24])
    if not width or not height or width > 32768 or height > 32768 or width * height > 268_435_456:
        raise CaptureError("frame dimensions exceed accepted bounds")
    return width, height


def read_samples(path: Path) -> list[dict]:
    if path.is_symlink() or not path.is_file():
        raise CaptureError("resource sample file is missing or is a symlink")
    raw = path.read_bytes()
    if raw and not raw.endswith(b"\n"):
        raise CaptureError("resource sample file has an incomplete final line")
    samples = []
    for line_number, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            sample = json.loads(line)
        except json.JSONDecodeError as error:
            raise CaptureError(f"resource sample line {line_number} is malformed") from error
        if not isinstance(sample, dict):
            raise CaptureError(f"resource sample line {line_number} is not an object")
        clean = {"ms": finite_number(sample.get("ms"), f"sample {line_number} ms")}
        for field in ("cpuPct", "ramMb", "diskPct"):
            if field in sample:
                clean[field] = finite_number(sample[field], f"sample {line_number} {field}")
        disks = sample.get("disks", [])
        if not isinstance(disks, list):
            raise CaptureError(f"resource sample line {line_number} disks is not an array")
        clean_disks = []
        for disk in disks:
            if not isinstance(disk, dict) or not isinstance(disk.get("name"), str) or not disk["name"]:
                raise CaptureError(f"resource sample line {line_number} has invalid disk metadata")
            clean_disk = {"name": disk["name"]}
            for field in ("writeMBs", "readMBs"):
                if field in disk:
                    clean_disk[field] = finite_number(disk[field], f"sample {line_number} {field}")
            clean_disks.append(clean_disk)
        if clean_disks:
            clean["disks"] = clean_disks
        samples.append(clean)
    if not samples:
        raise CaptureError("resource sample file contains no samples")
    if any(samples[i]["ms"] <= samples[i - 1]["ms"] for i in range(1, len(samples))):
        raise CaptureError("resource sample timestamps are not strictly increasing")
    return samples


def validate_capture(run_directory: Path, actor_version: str, actor_revision: str) -> dict:
    if run_directory.is_symlink() or not run_directory.is_dir():
        raise CaptureError("capture run directory is missing or is a symlink")
    if not isinstance(actor_version, str) or not ACTOR_VERSION.fullmatch(actor_version):
        raise CaptureError("actor version must be an explicit semantic version")
    if not isinstance(actor_revision, str) or not GIT_REVISION.fullmatch(actor_revision):
        raise CaptureError("actor revision must be a full lowercase Git SHA")
    run_dir = run_directory.resolve(strict=True)
    if not RUN_ID.fullmatch(run_dir.name):
        raise CaptureError("capture run directory name is invalid")

    status = read_json(run_dir / "capture-status.json")
    meta = read_json(run_dir / "capture-meta.json")
    capture = read_json(run_dir / "capture.json")
    if not isinstance(status, dict) or status.get("schema") != STATUS_SCHEMA:
        raise CaptureError("capture status schema is unsupported")
    if status.get("state") != "stopped" or status.get("captureJsonReady") is not True:
        raise CaptureError("capture is not finalized")
    if status.get("runDir") != str(run_dir):
        raise CaptureError("capture status run directory mismatch")
    for field in ("startedAt", "stoppedAt"):
        if not isinstance(status.get(field), str):
            raise CaptureError(f"capture status {field} is missing")
    try:
        started = datetime.fromisoformat(status["startedAt"].replace("Z", "+00:00"))
        stopped = datetime.fromisoformat(status["stoppedAt"].replace("Z", "+00:00"))
    except ValueError as error:
        raise CaptureError("capture status timestamps are invalid") from error
    if started.tzinfo is None or stopped.tzinfo is None or stopped < started:
        raise CaptureError("capture status timestamp ordering is invalid")
    if not isinstance(meta, dict) or any(not isinstance(meta.get(key), str) or not meta[key] for key in ("workload", "plane", "source")):
        raise CaptureError("capture metadata is missing workload, plane, or source")
    if not isinstance(capture, dict) or capture.get("schema") != CAPTURE_SCHEMA:
        raise CaptureError("capture manifest schema is unsupported")
    for key in ("workload", "plane", "source"):
        if capture.get(key) != meta[key]:
            raise CaptureError(f"capture manifest {key} mismatch")

    frames = capture.get("frames")
    count = capture.get("frameCount")
    if not isinstance(frames, list) or type(count) is not int or count < 1 or count != len(frames):
        raise CaptureError("capture frame count is inconsistent")
    if status.get("frameCount") != count:
        raise CaptureError("capture status frame count mismatch")
    fingerprints = []
    dimensions: set[tuple[int, int]] = set()
    last_ms = -1.0
    for index, frame in enumerate(frames):
        if not isinstance(frame, dict) or frame.get("index") != index:
            raise CaptureError("capture frame indices are not contiguous")
        ms = finite_number(frame.get("tMs"), f"frame {index} tMs")
        if ms < last_ms:
            raise CaptureError("capture frame timestamps are not monotonic")
        last_ms = ms
        filename = frame.get("image")
        if not isinstance(filename, str) or not FRAME_NAME.fullmatch(filename):
            raise CaptureError("capture frame filename is invalid")
        image = run_dir / filename
        if image.parent != run_dir or image.is_symlink():
            raise CaptureError("capture frame path escapes the run directory")
        width, height = png_dimensions(image)
        size = image.stat().st_size
        if type(frame.get("imageBytes")) is not int or frame["imageBytes"] != size:
            raise CaptureError(f"frame {index} image size mismatch")
        dhash = frame.get("dhashHex")
        if dhash is not None and (not isinstance(dhash, str) or not re.fullmatch(r"[0-9a-fA-F]{16}", dhash)):
            raise CaptureError(f"frame {index} dhashHex is invalid")
        dimensions.add((width, height))
        fingerprints.append({
            "index": index,
            "tMs": int(ms),
            "frameFile": filename,
            "sha256": sha256_file(image),
            "width": width,
            "height": height,
            "dhashHex": dhash.lower() if isinstance(dhash, str) else None,
        })

    duration = capture.get("durationMs")
    if type(duration) is not int or duration < 0 or int(last_ms) != duration:
        raise CaptureError("capture duration does not match final frame timestamp")
    if type(status.get("durationMs")) is not int or status["durationMs"] != duration:
        raise CaptureError("capture status duration mismatch")
    screen = None
    if len(dimensions) == 1:
        width, height = next(iter(dimensions))
        screen = {"width": width, "height": height}
    if capture.get("screen") is not None:
        declared = capture["screen"]
        if not isinstance(declared, dict) or set(declared) != {"width", "height"} or dimensions != {(declared["width"], declared["height"])}:
            raise CaptureError("capture screen dimensions mismatch")

    samples = read_samples(run_dir / "resources.jsonl")
    if type(status.get("samples")) is not int or status["samples"] != len(samples):
        raise CaptureError("capture status sample count mismatch")
    video_path = run_dir / "visible-desktop.webm"
    video = None
    if meta["source"] == "gnome-shell-screencast" and (video_path.is_symlink() or not video_path.is_file() or not video_path.stat().st_size):
        raise CaptureError("GNOME screencast video is missing or empty")
    if video_path.is_file() and not video_path.is_symlink():
        video = {"bytes": video_path.stat().st_size, "sha256": sha256_file(video_path)}

    deterministic = {
        "schema": EVIDENCE_SCHEMA,
        "actorVersion": actor_version,
        "actorRevision": actor_revision,
        "workload": meta["workload"],
        "plane": meta["plane"],
        "source": meta["source"],
        "screen": screen,
        "frames": [{key: frame[key] for key in ("index", "sha256", "width", "height")} for frame in fingerprints],
        "videoSha256": video["sha256"] if video else None,
    }
    identity_hash = hashlib.sha256(json.dumps(deterministic, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {
        "schema": EVIDENCE_SCHEMA,
        "runId": run_dir.name,
        "actorVersion": actor_version,
        "actorRevision": actor_revision,
        "captureState": "stopped",
        "startedAt": status["startedAt"],
        "stoppedAt": status["stoppedAt"],
        "workload": meta["workload"],
        "plane": meta["plane"],
        "source": meta["source"],
        "frameCount": count,
        "durationMs": duration,
        "screen": screen,
        "resourceSampleCount": len(samples),
        "resourceSamples": samples,
        "diskNames": sorted({disk["name"] for sample in samples for disk in sample.get("disks", [])}),
        "frames": fingerprints,
        "video": video,
        "deterministicIdentitySha256": identity_hash,
        "dhashFrameCount": sum(frame["dhashHex"] is not None for frame in fingerprints),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_directory", type=Path)
    parser.add_argument("--actor-version", required=True)
    parser.add_argument("--actor-revision", required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(validate_capture(args.run_directory, args.actor_version, args.actor_revision), sort_keys=True, separators=(",", ":")))
        return 0
    except CaptureError as error:
        print(f"assisted-benchmark-capture: refused: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())