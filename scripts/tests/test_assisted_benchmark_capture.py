from __future__ import annotations

import importlib.util
import json
import struct
import zlib
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY / "scripts" / "assisted_benchmark_capture.py"
ACTOR_VERSION = "1.4.14"
ACTOR_REVISION = "7f65a2215b952e1f31bc3677087f14c8b528d789"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def load_module():
    spec = importlib.util.spec_from_file_location("assisted_benchmark_capture_test", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load capture reader from {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def png(width: int, height: int, color: int) -> bytes:
    def chunk(name: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + name + data + struct.pack(">I", zlib.crc32(name + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    scanline = b"\x00" + bytes([color, 0, 255 - color]) * width
    image_data = zlib.compress(scanline * height)
    return PNG_SIGNATURE + chunk(b"IHDR", header) + chunk(b"IDAT", image_data) + chunk(b"IEND", b"")


def make_capture(root: Path, *, state: str = "stopped", source: str = "gnome-shell-screencast") -> Path:
    root.mkdir()
    frames = []
    for index, color in enumerate((16, 32)):
        name = f"frame-{index + 1:05d}.png"
        image = png(3, 2, color)
        (root / name).write_bytes(image)
        frames.append({
            "index": index,
            "tMs": index * 1000,
            "timingTicks64": str(index * 10_000_000),
            "image": name,
            "imageBytes": len(image),
            "dhashHex": None,
        })

    started_at = "2026-11-10T12:00:00Z"
    stopped_at = "2026-11-10T12:00:02Z"
    (root / "capture-meta.json").write_text(json.dumps({
        "workload": "labview-launch", "plane": "LINUX", "source": source,
    }), encoding="utf-8")
    (root / "capture-status.json").write_text(json.dumps({
        "schema": "labview-benchmark-actor/capture-status@1",
        "state": state,
        "runDir": str(root.resolve()),
        "startedAt": started_at,
        "stoppedAt": stopped_at if state == "stopped" else None,
        "frameCount": len(frames),
        "durationMs": 1000,
        "samples": 2,
        "captureJsonReady": state == "stopped",
    }), encoding="utf-8")
    (root / "capture.json").write_text(json.dumps({
        "schema": "labview-benchmark-actor/launch-capture@1",
        "workload": "labview-launch",
        "plane": "LINUX",
        "source": source,
        "fps": 1,
        "startMs": 1794312000000,
        "frameCount": len(frames),
        "durationMs": 1000,
        "screen": None,
        "frames": frames,
    }), encoding="utf-8")
    with (root / "resources.jsonl").open("w", encoding="utf-8") as handle:
        handle.write(json.dumps({"ms": 1794312000000, "cpuPct": 20, "ramMb": 1024, "diskPct": 4, "disks": []}) + "\n")
        handle.write(json.dumps({"ms": 1794312001000, "cpuPct": 25, "ramMb": 1030, "diskPct": 5, "disks": []}) + "\n")
    if source == "gnome-shell-screencast":
        (root / "visible-desktop.webm").write_bytes(b"video-fixture")
    return root


def validate(module, run_dir):
    return module.validate_capture(run_dir, ACTOR_VERSION, ACTOR_REVISION)


def test_stopped_capture_validates_and_derives_deterministic_frame_fingerprints(tmp_path: Path):
    module = load_module()
    run_dir = make_capture(tmp_path / "run-123")

    evidence = validate(module, run_dir)

    assert evidence["schema"] == "projectbuddy/assisted-benchmark-evidence@1"
    assert evidence["runId"] == "run-123"
    assert evidence["actorVersion"] == ACTOR_VERSION
    assert evidence["actorRevision"] == ACTOR_REVISION
    assert evidence["captureState"] == "stopped"
    assert evidence["screen"] == {"width": 3, "height": 2}
    assert evidence["frameCount"] == 2
    assert evidence["resourceSampleCount"] == 2
    assert evidence["resourceSamples"][0]["cpuPct"] == 20
    assert evidence["dhashFrameCount"] == 0
    assert all(len(frame["sha256"]) == 64 for frame in evidence["frames"])
    assert evidence["video"]["sha256"]
    assert str(run_dir) not in json.dumps(evidence)


def test_deterministic_identity_ignores_machine_variable_resource_samples(tmp_path: Path):
    module = load_module()
    run_dir = make_capture(tmp_path / "run-123")
    first = validate(module, run_dir)
    samples_path = run_dir / "resources.jsonl"
    samples = [json.loads(line) for line in samples_path.read_text(encoding="utf-8").splitlines()]
    samples[0]["cpuPct"] = 99
    samples_path.write_text("".join(json.dumps(sample) + "\n" for sample in samples), encoding="utf-8")
    second = validate(module, run_dir)
    assert first["deterministicIdentitySha256"] == second["deterministicIdentitySha256"]
    assert first["frames"] == second["frames"]


def test_changed_frame_changes_deterministic_identity(tmp_path: Path):
    module = load_module()
    run_dir = make_capture(tmp_path / "run-123")
    first = validate(module, run_dir)
    image_path = run_dir / "frame-00001.png"
    replacement = png(3, 2, 48)
    image_path.write_bytes(replacement)
    manifest_path = run_dir / "capture.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["frames"][0]["imageBytes"] = len(replacement)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    second = validate(module, run_dir)
    assert first["deterministicIdentitySha256"] != second["deterministicIdentitySha256"]


def test_capture_still_recording_is_not_accepted(tmp_path: Path):
    module = load_module()
    with pytest.raises(module.CaptureError, match="not finalized"):
        validate(module, make_capture(tmp_path / "run-123", state="capturing"))


def test_status_frame_count_mismatch_fails_closed(tmp_path: Path):
    module = load_module()
    run_dir = make_capture(tmp_path / "run-123")
    status_path = run_dir / "capture-status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    status["frameCount"] = 99
    status_path.write_text(json.dumps(status), encoding="utf-8")
    with pytest.raises(module.CaptureError, match="frame count"):
        validate(module, run_dir)


def test_frame_path_traversal_is_rejected(tmp_path: Path):
    module = load_module()
    run_dir = make_capture(tmp_path / "run-123")
    manifest_path = run_dir / "capture.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["frames"][0]["image"] = "../outside.png"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(module.CaptureError, match="filename is invalid"):
        validate(module, run_dir)


def test_partial_resource_line_is_rejected(tmp_path: Path):
    module = load_module()
    run_dir = make_capture(tmp_path / "run-123")
    with (run_dir / "resources.jsonl").open("ab") as handle:
        handle.write(b'{"ms":')
    with pytest.raises(module.CaptureError, match="incomplete final line"):
        validate(module, run_dir)


def test_status_sample_count_mismatch_is_rejected(tmp_path: Path):
    module = load_module()
    run_dir = make_capture(tmp_path / "run-123")
    status_path = run_dir / "capture-status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    status["samples"] = 1
    status_path.write_text(json.dumps(status), encoding="utf-8")
    with pytest.raises(module.CaptureError, match="sample count"):
        validate(module, run_dir)