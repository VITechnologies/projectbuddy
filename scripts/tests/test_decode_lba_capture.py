"""Real, assertion-based unit tests for Decode-LbaCapture.py's capture-summary
logic -- the genuinely unit-testable parts of this tool (it is a pure,
offline reader of already-written JSON; no live VS Code extension or VM is
needed to exercise it meaningfully). Uses small, synthetic fixture capture
directories, never this project's own real multi-gigabyte capture data.

Run with: pytest --cov=Decode-LbaCapture --cov-report=xml scripts/tests/
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parent.parent / "Decode-LbaCapture.py"
_spec = importlib.util.spec_from_file_location("decode_lba_capture", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
decode_lba_capture = importlib.util.module_from_spec(_spec)
sys.modules["decode_lba_capture"] = decode_lba_capture
_spec.loader.exec_module(decode_lba_capture)


def _frame(index: int, *, cpu=50.0, ram=1000.0, disk_pct=0.0, disks=None, dhash=None, image=None) -> dict:
    return {
        "index": index,
        "tMs": index * 83,
        "cpuPct": cpu,
        "ramMb": ram,
        "diskPct": disk_pct,
        "image": image or f"frame-{index:05d}.png",
        "imageBytes": 12345,
        "dhashHex": dhash,
        "disks": disks or [],
    }


def _capture(frames: list[dict], **overrides) -> dict:
    base = {
        "schema": decode_lba_capture.LAUNCH_CAPTURE_SCHEMA,
        "workload": "labview-launch",
        "plane": "LINUX",
        "source": "gnome-shell-screencast",
        "fps": 12,
        "startMs": 0,
        "frameCount": len(frames),
        "durationMs": frames[-1]["tMs"] if frames else 0,
        "screen": None,
        "frames": frames,
        "dualPacket": {
            "schema": "labview-benchmark-actor/mprr-dual-packet@v1",
            "outcome": "authoritative",
            "authoritative": True,
            "frameCount": len(frames),
            "authoritativeFrames": len(frames),
            "frames": [{"frameIndex": f["index"], "outcome": "authoritative"} for f in frames],
        },
        "diskNames": [],
    }
    base.update(overrides)
    return base


def _status(**overrides) -> dict:
    base = {
        "schema": decode_lba_capture.CAPTURE_STATUS_SCHEMA,
        "state": "stopped",
        "frameCount": 1,
        "durationMs": 1000,
    }
    base.update(overrides)
    return base


# --- find_latest_run_dir ------------------------------------------------

def test_find_latest_run_dir_picks_highest_timestamp(tmp_path: Path):
    (tmp_path / "run-100").mkdir()
    (tmp_path / "run-300").mkdir()
    (tmp_path / "run-200").mkdir()
    assert decode_lba_capture.find_latest_run_dir(tmp_path) == tmp_path / "run-300"


def test_find_latest_run_dir_ignores_non_matching_entries(tmp_path: Path):
    (tmp_path / "run-100").mkdir()
    (tmp_path / "not-a-run-dir").mkdir()
    (tmp_path / "run-100-extra").mkdir()  # does not match ^run-(\d+)$
    assert decode_lba_capture.find_latest_run_dir(tmp_path) == tmp_path / "run-100"


def test_find_latest_run_dir_raises_if_root_missing(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="Captures root not found"):
        decode_lba_capture.find_latest_run_dir(tmp_path / "does-not-exist")


def test_find_latest_run_dir_raises_if_no_run_dirs(tmp_path: Path):
    (tmp_path / "unrelated").mkdir()
    with pytest.raises(FileNotFoundError, match="No 'run-<timestamp>'"):
        decode_lba_capture.find_latest_run_dir(tmp_path)


# --- _load_json_checked --------------------------------------------------

def test_load_json_checked_matching_schema_has_no_warning(tmp_path: Path):
    path = tmp_path / "capture.json"
    path.write_text(json.dumps({"schema": "x@1", "a": 1}), encoding="utf-8")
    data, warning = decode_lba_capture._load_json_checked(path, "x@1")
    assert data == {"schema": "x@1", "a": 1}
    assert warning is None


def test_load_json_checked_mismatched_schema_warns_but_still_parses(tmp_path: Path):
    path = tmp_path / "capture.json"
    path.write_text(json.dumps({"schema": "x@2", "a": 1}), encoding="utf-8")
    data, warning = decode_lba_capture._load_json_checked(path, "x@1")
    assert data == {"schema": "x@2", "a": 1}
    assert warning is not None
    assert "x@2" in warning and "x@1" in warning


# --- summarize_capture: resource stats -----------------------------------

def test_summarize_capture_computes_cpu_ram_disk_min_max_avg():
    frames = [_frame(0, cpu=10.0, ram=100.0, disk_pct=0.0), _frame(1, cpu=30.0, ram=200.0, disk_pct=50.0)]
    summary = decode_lba_capture.summarize_capture(_capture(frames), None, [])
    assert summary["resources"]["cpuPct"] == {"min": 10.0, "max": 30.0, "avg": 20.0}
    assert summary["resources"]["ramMb"] == {"min": 100.0, "max": 200.0, "avg": 150.0}
    assert summary["resources"]["diskPct"] == {"min": 0.0, "max": 50.0, "avg": 25.0}


def test_summarize_capture_handles_empty_frames_gracefully():
    summary = decode_lba_capture.summarize_capture(_capture([]), None, [])
    assert summary["resources"]["cpuPct"] is None
    assert summary["peakIo"] is None
    assert summary["representativeFrames"] == {}


# --- summarize_capture: per-disk peak I/O --------------------------------

def test_summarize_capture_finds_peak_write_disk_and_frame():
    frames = [
        _frame(0, disks=[{"name": "nvme0n1", "writeMBs": 1.0, "readMBs": 0.0}]),
        _frame(1, disks=[{"name": "nvme0n1", "writeMBs": 9.5, "readMBs": 2.0}]),
        _frame(2, disks=[{"name": "nvme0n1", "writeMBs": 0.5, "readMBs": 0.0}]),
    ]
    summary = decode_lba_capture.summarize_capture(_capture(frames), None, [])
    assert summary["peakIo"] == {"disk": "nvme0n1", "writeMBs": 9.5, "readMBs": 2.0, "frameIndex": 1, "tMs": 83}
    assert summary["representativeFrames"]["peakIo"] == "frame-00001.png"


def test_summarize_capture_ignores_disk_entries_with_no_name():
    frames = [_frame(0, disks=[{"name": None, "writeMBs": 99.0}, {"name": "a", "writeMBs": 1.0}])]
    summary = decode_lba_capture.summarize_capture(_capture(frames), None, [])
    assert list(summary["resources"]["disks"].keys()) == ["a"]


def test_summarize_capture_per_disk_stats_cover_every_named_disk():
    frames = [
        _frame(0, disks=[{"name": "a", "writeMBs": 1.0, "readMBs": 2.0}, {"name": "b", "writeMBs": 0.0, "readMBs": 0.0}]),
        _frame(1, disks=[{"name": "a", "writeMBs": 3.0, "readMBs": 4.0}, {"name": "b", "writeMBs": 0.0, "readMBs": 0.0}]),
    ]
    summary = decode_lba_capture.summarize_capture(_capture(frames), None, [])
    assert summary["resources"]["disks"]["a"]["writeMBs"] == {"min": 1.0, "max": 3.0, "avg": 2.0}
    assert summary["resources"]["disks"]["b"]["writeMBs"] == {"min": 0.0, "max": 0.0, "avg": 0.0}


# --- summarize_capture: representative frames ----------------------------

def test_summarize_capture_representative_frames_first_and_last():
    frames = [_frame(0, image="frame-00001.png"), _frame(1, image="frame-00002.png"), _frame(2, image="frame-00003.png")]
    summary = decode_lba_capture.summarize_capture(_capture(frames), None, [])
    assert summary["representativeFrames"]["first"] == "frame-00001.png"
    assert summary["representativeFrames"]["last"] == "frame-00003.png"


# --- summarize_capture: dhashHex, per ADR-0005's Addendum -----------------
# (null on every frame is NORMAL for the real "Capture LabVIEW Launch"
# command -- only a MIXED population within one capture is a genuine
# anomaly; a uniformly-null or uniformly-populated capture is not flagged.)

def test_summarize_capture_dhash_uniformly_null_is_not_an_anomaly():
    frames = [_frame(0, dhash=None), _frame(1, dhash=None)]
    summary = decode_lba_capture.summarize_capture(_capture(frames), None, [])
    assert summary["dhash"] == {"populatedFrames": 0, "totalFrames": 2}
    assert summary["anomalies"] == []


def test_summarize_capture_dhash_uniformly_populated_is_not_an_anomaly():
    frames = [_frame(0, dhash="abc123"), _frame(1, dhash="def456")]
    summary = decode_lba_capture.summarize_capture(_capture(frames), None, [])
    assert summary["dhash"] == {"populatedFrames": 2, "totalFrames": 2}
    assert summary["anomalies"] == []


def test_summarize_capture_dhash_mixed_population_is_flagged():
    frames = [_frame(0, dhash="abc123"), _frame(1, dhash=None)]
    summary = decode_lba_capture.summarize_capture(_capture(frames), None, [])
    assert summary["dhash"] == {"populatedFrames": 1, "totalFrames": 2}
    assert any("inconsistent" in a for a in summary["anomalies"])


def test_summarize_capture_null_screen_is_not_an_anomaly():
    # Per ADR-0005's Addendum: screen is only populated from optional
    # capture-meta.json screenW/screenH fields the real capture command
    # never emits -- null is the structurally expected value, not a flag.
    summary = decode_lba_capture.summarize_capture(_capture([_frame(0)], screen=None), None, [])
    assert summary["screen"] is None
    assert summary["anomalies"] == []


# --- summarize_capture: dualPacket integrity ------------------------------

def test_summarize_capture_authoritative_dual_packet_is_not_flagged():
    frames = [_frame(0)]
    summary = decode_lba_capture.summarize_capture(_capture(frames), None, [])
    assert summary["dualPacket"]["outcome"] == "authoritative"
    assert summary["anomalies"] == []


def test_summarize_capture_degraded_dual_packet_is_flagged_with_frame_indices():
    frames = [_frame(0), _frame(1)]
    capture = _capture(frames, dualPacket={
        "outcome": "degraded-long-deferred",
        "authoritative": False,
        "frameCount": 2,
        "authoritativeFrames": 1,
        "frames": [
            {"frameIndex": 0, "outcome": "authoritative"},
            {"frameIndex": 1, "outcome": "failed"},
        ],
    })
    summary = decode_lba_capture.summarize_capture(capture, None, [])
    assert any("degraded-long-deferred" in a and "[1]" in a for a in summary["anomalies"])


# --- summarize_capture: capture-status.json state -------------------------

def test_summarize_capture_stopped_status_is_not_flagged():
    summary = decode_lba_capture.summarize_capture(_capture([_frame(0)]), _status(state="stopped"), [])
    assert summary["anomalies"] == []


def test_summarize_capture_failed_status_is_flagged_with_error():
    status = _status(state="failed", error="ffmpeg exited non-zero")
    summary = decode_lba_capture.summarize_capture(_capture([_frame(0)]), status, [])
    assert any("failed" in a and "ffmpeg exited non-zero" in a for a in summary["anomalies"])


def test_summarize_capture_capturing_status_is_flagged():
    summary = decode_lba_capture.summarize_capture(_capture([_frame(0)]), _status(state="capturing"), [])
    assert any("capturing" in a for a in summary["anomalies"])


def test_summarize_capture_unrecognized_status_state_is_flagged():
    summary = decode_lba_capture.summarize_capture(_capture([_frame(0)]), _status(state="unknown-future-state"), [])
    assert any("unrecognized state" in a for a in summary["anomalies"])


def test_summarize_capture_missing_status_beacon_is_not_flagged():
    summary = decode_lba_capture.summarize_capture(_capture([_frame(0)]), None, [])
    assert summary["statusBeacon"] is None
    assert summary["anomalies"] == []


# --- cmd_summarize / CLI wiring -------------------------------------------

def _write_capture_dir(tmp_path: Path, frames: list[dict], status: dict | None = None) -> Path:
    run_dir = tmp_path / "run-123"
    run_dir.mkdir()
    (run_dir / "capture.json").write_text(json.dumps(_capture(frames)), encoding="utf-8")
    if status is not None:
        (run_dir / "capture-status.json").write_text(json.dumps(status), encoding="utf-8")
    return run_dir


def test_cmd_summarize_missing_capture_json_fails_closed(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    run_dir = tmp_path / "run-123"
    run_dir.mkdir()
    exit_code = decode_lba_capture.cmd_summarize(run_dir, None)
    assert exit_code == 2
    assert "capture.json not found" in capsys.readouterr().err


def test_cmd_summarize_writes_json_file(tmp_path: Path):
    run_dir = _write_capture_dir(tmp_path, [_frame(0)])
    out_json = tmp_path / "out.json"
    exit_code = decode_lba_capture.cmd_summarize(run_dir, out_json)
    assert exit_code == 0
    written = json.loads(out_json.read_text(encoding="utf-8"))
    assert written["runDir"] == str(run_dir)
    assert written["frameCount"] == 1


def test_cmd_summarize_prints_to_stdout_when_no_json_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    run_dir = _write_capture_dir(tmp_path, [_frame(0)])
    exit_code = decode_lba_capture.cmd_summarize(run_dir, None)
    assert exit_code == 0
    assert '"workload": "labview-launch"' in capsys.readouterr().out


def test_cmd_summarize_includes_status_beacon_when_present(tmp_path: Path):
    run_dir = _write_capture_dir(tmp_path, [_frame(0)], status=_status(state="stopped"))
    out_json = tmp_path / "out.json"
    decode_lba_capture.cmd_summarize(run_dir, out_json)
    written = json.loads(out_json.read_text(encoding="utf-8"))
    assert written["statusBeacon"]["state"] == "stopped"


def test_cmd_summarize_warns_on_capture_json_schema_mismatch(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    run_dir = tmp_path / "run-123"
    run_dir.mkdir()
    (run_dir / "capture.json").write_text(json.dumps(_capture([_frame(0)], schema="other@2")), encoding="utf-8")
    exit_code = decode_lba_capture.cmd_summarize(run_dir, None)
    assert exit_code == 0
    assert "other@2" in capsys.readouterr().err


def test_cmd_summarize_warns_on_capture_status_schema_mismatch(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    run_dir = _write_capture_dir(tmp_path, [_frame(0)], status={"schema": "other-status@2", "state": "stopped"})
    exit_code = decode_lba_capture.cmd_summarize(run_dir, None)
    assert exit_code == 0
    assert "other-status@2" in capsys.readouterr().err


def _run_cli(monkeypatch: pytest.MonkeyPatch, *args: str) -> int:
    monkeypatch.setattr(sys, "argv", ["Decode-LbaCapture.py", *args])
    return decode_lba_capture.main()


def test_cli_explicit_run_dir_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    run_dir = _write_capture_dir(tmp_path, [_frame(0)])
    out_json = tmp_path / "out.json"
    exit_code = _run_cli(monkeypatch, str(run_dir), "--json", str(out_json))
    assert exit_code == 0
    assert out_json.exists()


def test_cli_missing_explicit_run_dir_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    exit_code = _run_cli(monkeypatch, str(tmp_path / "does-not-exist"))
    assert exit_code == 2
    assert "run_dir not found" in capsys.readouterr().err


def test_cli_default_discovery_picks_latest_run_dir_under_captures_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    captures_root = tmp_path / "captures"
    captures_root.mkdir()
    _write_capture_dir(captures_root, [_frame(0)])
    (captures_root / "run-123").rename(captures_root / "run-999")
    exit_code = _run_cli(monkeypatch, "--captures-root", str(captures_root))
    assert exit_code == 0


def test_cli_default_discovery_fails_closed_on_empty_captures_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    captures_root = tmp_path / "captures"
    captures_root.mkdir()
    exit_code = _run_cli(monkeypatch, "--captures-root", str(captures_root))
    assert exit_code == 2
    assert "No 'run-<timestamp>'" in capsys.readouterr().err


# --- default_captures_root -------------------------------------------------

def test_default_captures_root_ends_with_expected_path_segments():
    root = decode_lba_capture.default_captures_root()
    parts = root.parts
    assert parts[-1] == "captures"
    assert parts[-2] == decode_lba_capture.EXTENSION_STORAGE_ID
    assert parts[-3] == "globalStorage"


def test_default_captures_root_on_windows(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", r"C:\Users\someone\AppData\Roaming")
    root = decode_lba_capture.default_captures_root()
    assert "AppData" in str(root) or "Roaming" in str(root)
    assert root.parts[-1] == "captures"


def test_default_captures_root_on_macos(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    root = decode_lba_capture.default_captures_root()
    assert "Library" in root.parts
    assert root.parts[-1] == "captures"
