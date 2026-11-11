from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
CONTRACT_PATH = REPOSITORY / "tools" / "assisted-benchmark" / "lba-mcp-contract.json"
CONTRACT_MODULE = REPOSITORY / "scripts" / "assisted_benchmark_contract.py"
REVIEW_MODULE = REPOSITORY / "scripts" / "assisted_benchmark_review.py"
MODEL = {
    "tag": "projectbuddy:0.1-qwen2.5-7b",
    "digest": "845dbda0ea48ed749caafd9e6037047aa19acfcfd82e704d7ca97d631a0b697e",
}


def load_module(name: str, path: Path):
    scripts_path = str(REPOSITORY / "scripts")
    if scripts_path not in sys.path:
        sys.path.insert(0, scripts_path)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load test module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def contract():
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def authorization(fields=("captureMetadata", "resourceSamples"), **overrides):
    value = {
        "schema": "projectbuddy/assisted-benchmark-authorization@1",
        "runId": "run-0001",
        "workload": "labview-launch",
        "sourceRevision": "7f65a2215b952e1f31bc3677087f14c8b528d789",
        "approvedBy": "operator",
        "approvedAt": "2026-11-10T12:00:00Z",
        "approvedCalls": [{"tool": "get_benchmark_series", "arguments": {}}],
        "targetVmUuids": [],
        "ollamaModel": dict(MODEL),
        "evidenceFields": list(fields),
        "allowVmLifecycle": False,
        "teardownOnNativePplFailure": False,
    }
    value.update(overrides)
    return value


def evidence():
    return {
        "schema": "projectbuddy/assisted-benchmark-evidence@1",
        "runId": "run-0001",
        "captureState": "stopped",
        "workload": "labview-launch",
        "plane": "LINUX",
        "source": "gnome-shell-screencast",
        "frameCount": 2,
        "durationMs": 1000,
        "screen": {"width": 1920, "height": 1080},
        "resourceSampleCount": 1,
        "resourceSamples": [{"ms": 1, "cpuPct": 30, "ramMb": 2048}],
        "frames": [{"index": 0, "sha256": "a" * 64, "width": 1920, "height": 1080, "dhashHex": None}],
        "video": {"bytes": 100, "sha256": "b" * 64},
        "deterministicIdentitySha256": "c" * 64,
    }


def assessment(decision="pass", findings=None):
    return {
        "decision": decision,
        "findings": findings or [],
        "uncertainties": [],
        "recommendation": "retain the verified receipt",
    }


class FakeTransport:
    def __init__(self, response=None, digests=None):
        self.response = response or assessment_response()
        self.digests = digests or [MODEL["digest"], MODEL["digest"]]
        self.tag_calls = 0
        self.payload = None
        self.payloads = []

    def tags(self):
        digest = self.digests[min(self.tag_calls, len(self.digests) - 1)]
        self.tag_calls += 1
        return {"models": [{"name": MODEL["tag"], "digest": digest}]}

    def show(self, model):
        return {"capabilities": ["completion", "vision"]}

    def chat(self, payload):
        self.payload = payload
        self.payloads.append(payload)
        return copy.deepcopy(self.response)


def assessment_response(model=None, content=None, done=True, reason="stop"):
    review = content or assessment()
    return {
        "model": model or MODEL["tag"],
        "done": done,
        "done_reason": reason,
        "message": {"content": json.dumps(review)},
    }


def test_review_uses_only_per_run_approved_evidence_and_returns_provenance():
    contract_module = load_module("assisted_contract_for_review", CONTRACT_MODULE)
    review_module = load_module("assisted_review_test", REVIEW_MODULE)
    contract_value = contract()
    transport = FakeTransport()

    result = review_module.review_evidence(evidence(), authorization(), contract_value, transport)

    assert result["schema"] == "projectbuddy/assisted-benchmark-assessment@1"
    assert result["runId"] == "run-0001"
    assert result["model"] == MODEL
    assert result["advisoryOnly"] is True
    assert transport.tag_calls == 2
    prompt_data = json.loads(transport.payload["messages"][1]["content"])
    assert set(prompt_data) == {"captureMetadata", "resourceSamples"}
    assert "frames" not in prompt_data


def test_review_refuses_evidence_from_a_different_run():
    contract_module = load_module("assisted_contract_for_mismatch", CONTRACT_MODULE)
    review_module = load_module("assisted_review_mismatch", REVIEW_MODULE)
    changed = evidence()
    changed["runId"] = "another-run"
    with pytest.raises(review_module.ReviewError, match="run ID"):
        review_module.select_evidence(changed, authorization(), contract())


def test_review_refuses_unavailable_or_unapproved_fields():
    review_module = load_module("assisted_review_fields", REVIEW_MODULE)
    with pytest.raises(review_module.ReviewError, match="unavailable"):
        review_module.select_evidence(evidence(), authorization(("benchmarkResult",)), contract())
    with pytest.raises(review_module.ContractError, match="unknown evidence fields"):
        review_module.select_evidence(evidence(), authorization(("rawImages",)), contract())


def test_review_refuses_model_digest_change_during_assessment():
    review_module = load_module("assisted_review_digest", REVIEW_MODULE)
    transport = FakeTransport(digests=[MODEL["digest"], "0" * 64])
    with pytest.raises(review_module.ReviewError, match="digest is not installed"):
        review_module.review_evidence(evidence(), authorization(), contract(), transport)


@pytest.mark.parametrize("bad", [
    assessment(decision="pass", findings=[{"severity": "blocking", "claim": "bad", "evidenceFields": ["captureMetadata"]}]),
    assessment(decision="fail"),
    assessment(decision="pass", findings=[{"severity": "info", "claim": "bad", "evidenceFields": ["rawImages"]}]),
    {"decision": "pass", "findings": [], "uncertainties": [], "recommendation": "ok", "command": "delete VM"},
])
def test_model_response_must_be_schema_valid_and_evidence_linked(bad):
    review_module = load_module(f"assisted_review_schema_{id(bad)}", REVIEW_MODULE)
    with pytest.raises(review_module.ReviewError):
        review_module.validate_assessment(bad, {"captureMetadata", "resourceSamples"})


def test_model_output_must_be_complete_and_use_the_approved_model():
    review_module = load_module("assisted_review_completion", REVIEW_MODULE)
    wrong_model = FakeTransport(response=assessment_response(model="other:tag"))
    with pytest.raises(review_module.ReviewError, match="incomplete or used a different model"):
        review_module.review_evidence(evidence(), authorization(), contract(), wrong_model)


def test_approved_images_are_hash_verified_and_sent_only_to_local_vision_model(tmp_path):
    review_module = load_module("assisted_review_images", REVIEW_MODULE)
    run_fixture = load_module("assisted_run_capture_fixture", REPOSITORY / "scripts" / "tests" / "test_assisted_benchmark_run.py")
    capture_directory = tmp_path / "run-0001"
    capture = run_fixture.make_capture(capture_directory)
    auth = authorization(("frameImages",))
    transport = FakeTransport()

    result = review_module.review_evidence(
        capture, auth, contract(), transport, capture_directory=capture_directory
    )

    user_message = transport.payload["messages"][1]
    text_evidence = json.loads(user_message["content"])
    assert len(user_message["images"]) == 1
    assert "_imagesBase64" not in json.dumps(text_evidence)
    assert str(capture_directory) not in user_message["content"]
    assert result["advisoryOnly"] is True


def test_approved_images_are_refused_for_a_text_only_model(tmp_path):
    review_module = load_module("assisted_review_images_text_only", REVIEW_MODULE)
    run_fixture = load_module("assisted_run_capture_fixture_text_only", REPOSITORY / "scripts" / "tests" / "test_assisted_benchmark_run.py")
    capture_directory = tmp_path / "run-0001"
    capture = run_fixture.make_capture(capture_directory)
    auth = authorization(("frameImages",))

    class TextOnlyOllama(FakeTransport):
        def show(self, model):
            return {"capabilities": ["completion"]}

    with pytest.raises(review_module.ReviewError, match="vision-capable"):
        review_module.review_evidence(
            capture, auth, contract(), TextOnlyOllama(), capture_directory=capture_directory
        )


def test_multiple_approved_frames_use_separate_single_image_requests(tmp_path):
    review_module = load_module("assisted_review_sequential_images", REVIEW_MODULE)
    run_fixture = load_module("assisted_run_capture_sequential_images", REPOSITORY / "scripts" / "tests" / "test_assisted_benchmark_run.py")
    capture_directory = tmp_path / "run-0001"
    capture = run_fixture.make_capture(capture_directory)
    base_frame = capture["frames"][0]
    frame_bytes = (capture_directory / base_frame["frameFile"]).read_bytes()
    frames = []
    for index in range(3):
        filename = f"frame-{index + 1:05d}.png"
        (capture_directory / filename).write_bytes(frame_bytes)
        frames.append({**base_frame, "index": index, "tMs": index * 1000, "frameFile": filename})
    capture["frames"] = frames
    capture["frameCount"] = 3
    auth = authorization(("frameImages",))
    transport = FakeTransport()

    result = review_module.review_evidence(
        capture, auth, contract(), transport, capture_directory=capture_directory
    )

    assert result["reviewedFrameCount"] == 3
    assert len(transport.payloads) == 3
    assert all(len(payload["messages"][1]["images"]) == 1 for payload in transport.payloads)
    assert [payload["keep_alive"] for payload in transport.payloads] == ["5m", "5m", 0]
    assert all(len(json.loads(payload["messages"][1]["content"])["frameImages"]["frames"]) == 1 for payload in transport.payloads)

    incomplete = FakeTransport(response=assessment_response(done=False))
    with pytest.raises(review_module.ReviewError, match="incomplete or used a different model"):
        review_module.review_evidence(evidence(), authorization(), contract(), incomplete)


@pytest.mark.parametrize("endpoint", ["http://example.com:11434", "http://127.0.0.1:11434/path", "http://user:pass@127.0.0.1:11434"])
def test_ollama_transport_refuses_non_loopback_or_credentialed_endpoint(endpoint):
    review_module = load_module(f"assisted_review_endpoint_{abs(hash(endpoint))}", REVIEW_MODULE)
    with pytest.raises(review_module.ReviewError):
        review_module.LocalOllama(endpoint)