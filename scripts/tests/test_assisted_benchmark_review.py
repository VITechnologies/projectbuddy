from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
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
        "reviewPrivacy": {"schema": "projectbuddy/ollama-privacy@1", "imageMode": "full-frame", "frames": [],
                  "retainModelText": False, "expiresAt": "2099-01-01T00:00:00Z"},
        "reviewBudget": {"schema": "projectbuddy/ollama-budget@1", "accounting": "utf8-byte-plus-approved-media@1",
                 "modelDigest": MODEL["digest"], "contextTokens": 16384, "outputTokens": 512,
                 "templateReserveTokens": 1024, "imageTokensPerImage": 2048,
                 "maxTextBytes": 65536, "maxImageBytes": 8 * 1024 * 1024, "maxImagesPerRequest": 1},
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
        return {"capabilities": ["completion", "vision"], "template": "", "system": "",
            "model_info": {"general.architecture": "fixture", "fixture.context_length": 32768,
                       "tokenizer.ggml.model": "gpt2"}}

    def chat(self, payload):
        self.payload = payload
        self.payloads.append(payload)
        response = copy.deepcopy(self.response)
        data = json.loads(payload["messages"][1]["content"])
        if "frameImages" in data:
            content = json.loads(response["message"]["content"])
            for finding in content.get("findings", []):
                finding.setdefault("frameRefs", [data["frameImages"]["frames"][0]["frameId"]]
                                   if "frameImages" in finding["evidenceFields"] else [])
            response["message"]["content"] = json.dumps(content)
        return response


def assessment_response(model=None, content=None, done=True, reason="stop"):
    review = content or assessment()
    return {
        "model": model or MODEL["tag"],
        "done": done,
        "done_reason": reason,
        "prompt_eval_count": 64,
        "eval_count": 16,
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
    assert result["approvedModel"] == MODEL
    assert result["advisoryOnly"] is True
    assert transport.tag_calls == 2
    prompt_data = json.loads(transport.payload["messages"][1]["content"])
    assert set(prompt_data) == {"captureMetadata", "resourceSamples"}
    assert "frames" not in prompt_data


def test_request_cannot_silently_exceed_its_context_budget():
    review_module = load_module("assisted_review_context_overflow", REVIEW_MODULE)
    with pytest.raises(review_module.ReviewError, match="budget"):
        review_module.build_review_request(MODEL["tag"], {"captureMetadata": {"description": "x" * 20000}})


def test_context_budget_exact_boundary_and_one_token_over():
    review_module = load_module("assisted_review_exact_budget", REVIEW_MODULE)
    budget = authorization()["reviewBudget"]
    selected = {"captureMetadata": {"description": "public fixture"}}
    payload = review_module.build_review_request(MODEL["tag"], selected, review_budget=budget)
    measured = review_module.request_budget(payload, budget)
    exact = {**budget, "contextTokens": measured["inputTokenUpperBound"] + budget["outputTokens"]}
    accepted = review_module.build_review_request(MODEL["tag"], selected, review_budget=exact)
    assert accepted["options"]["num_ctx"] == exact["contextTokens"]
    with pytest.raises(review_module.ReviewError, match="context budget"):
        review_module.build_review_request(MODEL["tag"], selected, review_budget={**exact, "contextTokens": exact["contextTokens"] - 1})


@pytest.mark.parametrize("field,value", [
    ("contextTokens", 0), ("outputTokens", True), ("templateReserveTokens", -1),
    ("maxTextBytes", 0), ("maxImageBytes", 0), ("imageTokensPerImage", 0),
    ("maxImagesPerRequest", 2), ("accounting", "guess"), ("modelDigest", "0" * 64),
])
def test_invalid_budget_prevents_any_inference(field, value):
    review_module = load_module("assisted_review_invalid_budget", REVIEW_MODULE)
    auth = authorization()
    auth["reviewBudget"][field] = value
    transport = FakeTransport()
    with pytest.raises(review_module.ContractError, match="budget"):
        review_module.review_evidence(evidence(), auth, contract(), transport)
    assert transport.payloads == []
    assert transport.tag_calls == 0


@pytest.mark.parametrize("problem", ["unknown-tokenizer", "small-context", "large-template", "missing-info"])
def test_unsupported_model_budget_metadata_prevents_inference(problem):
    review_module = load_module("assisted_review_model_budget", REVIEW_MODULE)

    class UnsupportedMetadata(FakeTransport):
        def show(self, model):
            metadata = super().show(model)
            if problem == "unknown-tokenizer":
                metadata["model_info"]["tokenizer.ggml.model"] = "unknown"
            elif problem == "small-context":
                metadata["model_info"]["fixture.context_length"] = 1024
            elif problem == "large-template":
                metadata["template"] = "x" * 2000
            else:
                metadata.pop("model_info")
            return metadata

    transport = UnsupportedMetadata()
    with pytest.raises(review_module.ReviewError, match="budget"):
        review_module.review_evidence(evidence(), authorization(), contract(), transport)
    assert transport.payloads == []


@pytest.mark.parametrize("field,value", [("prompt_eval_count", None), ("prompt_eval_count", True),
                                         ("prompt_eval_count", 999999), ("eval_count", 0), ("eval_count", 999999)])
def test_incomplete_or_over_budget_completion_is_not_an_assessment(field, value):
    review_module = load_module("assisted_review_completion_budget", REVIEW_MODULE)
    response = assessment_response()
    response[field] = value
    with pytest.raises(review_module.ReviewError, match="completion counts"):
        review_module.review_evidence(evidence(), authorization(), contract(), FakeTransport(response=response))


def test_image_payload_must_fit_the_explicit_media_budget():
    review_module = load_module("assisted_review_media_budget", REVIEW_MODULE)
    budget = {**authorization()["reviewBudget"], "maxImageBytes": 1}
    selected = {"frameImages": {"frames": [{"index": 0, "sha256": "a" * 64, "frameId": "f" * 64}], "_imagesBase64": ["YWJj"]}}
    with pytest.raises(review_module.ReviewError, match="image budget"):
        review_module.build_review_request(MODEL["tag"], selected, review_budget=budget)


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


def test_malformed_inventory_entry_prevents_inference():
    review_module = load_module("assisted_review_malformed_inventory", REVIEW_MODULE)

    class MalformedInventory(FakeTransport):
        def tags(self):
            return {"models": [None, {"name": MODEL["tag"], "digest": MODEL["digest"]}]}

    transport = MalformedInventory()
    with pytest.raises(review_module.ReviewError, match="inventory"):
        review_module.review_evidence(evidence(), authorization(), contract(), transport)
    assert transport.payloads == []


@pytest.mark.parametrize("entry", [
    {}, {"name": MODEL["tag"]}, {"digest": MODEL["digest"]},
    {"name": None, "digest": MODEL["digest"]},
    {"name": " ", "digest": MODEL["digest"]},
    {"name": "other:1", "digest": 3},
    {"name": "other:1", "digest": "abc"},
    {"name": "other:1", "digest": "g" * 64},
    {"name": MODEL["tag"], "model": "other:1", "digest": MODEL["digest"]},
    {"name": MODEL["tag"], "digest": MODEL["digest"]},
])
def test_invalid_or_duplicate_inventory_prevents_inference(entry):
    review_module = load_module("assisted_review_invalid_inventory", REVIEW_MODULE)

    class InvalidInventory(FakeTransport):
        def tags(self):
            return {"models": [entry, {"name": MODEL["tag"], "digest": MODEL["digest"]}]}

    transport = InvalidInventory()
    with pytest.raises(review_module.ReviewError):
        review_module.review_evidence(evidence(), authorization(), contract(), transport)
    assert transport.payloads == []


@pytest.mark.parametrize("inventory", [None, [], {}, {"models": None}, {"models": {}}, {"models": []}])
def test_missing_or_malformed_inventory_prevents_inference(inventory):
    review_module = load_module("assisted_review_missing_inventory", REVIEW_MODULE)

    class MissingInventory(FakeTransport):
        def tags(self):
            return inventory

    transport = MissingInventory()
    with pytest.raises(review_module.ReviewError):
        review_module.review_evidence(evidence(), authorization(), contract(), transport)
    assert transport.payloads == []


@pytest.mark.parametrize("approved_model", [
    None, {}, {"tag": MODEL["tag"]}, {"tag": MODEL["tag"], "digest": "abc"},
    {"tag": MODEL["tag"], "digest": "g" * 64},
])
def test_missing_or_invalid_model_approval_never_contacts_ollama(approved_model):
    review_module = load_module("assisted_review_invalid_approval", REVIEW_MODULE)
    transport = FakeTransport()
    with pytest.raises(review_module.ContractError):
        review_module.review_evidence(evidence(), authorization(ollamaModel=approved_model), contract(), transport)
    assert transport.tag_calls == 0
    assert transport.payloads == []


@pytest.mark.parametrize("postflight", ["malformed", "duplicate", "unavailable"])
def test_invalid_postflight_cannot_produce_an_assessment(postflight):
    review_module = load_module("assisted_review_postflight", REVIEW_MODULE)

    class InvalidPostflight(FakeTransport):
        def tags(self):
            if self.tag_calls == 0:
                return super().tags()
            if postflight == "unavailable":
                raise review_module.ReviewError("fixture endpoint unavailable")
            if postflight == "malformed":
                return {"models": [None]}
            return {"models": [{"name": MODEL["tag"], "digest": MODEL["digest"]}] * 2}

    transport = InvalidPostflight()
    with pytest.raises(review_module.ReviewError):
        review_module.review_evidence(evidence(), authorization(), contract(), transport)
    assert len(transport.payloads) == 1


@pytest.mark.parametrize("inventory", [
    '{"models":[],"models":[]}',
    '{"models":[{"name":"other:1","name":"approved:1"}]}',
    '{"models":[{"digest":"first","digest":"second"}]}',
])
def test_duplicate_json_inventory_fields_are_rejected_before_chat(inventory):
    review_module = load_module("assisted_review_duplicate_json", REVIEW_MODULE)
    transport = review_module.LocalOllama("http://127.0.0.1:11434")

    class InventoryOpener:
        def __init__(self):
            self.paths = []

        def open(self, request, timeout):
            self.paths.append(request.full_url)
            return io.BytesIO(inventory.encode())

    opener = InventoryOpener()
    transport.opener = opener
    with pytest.raises(review_module.ReviewError, match="duplicate JSON fields"):
        review_module.review_evidence(evidence(), authorization(), contract(), transport)
    assert opener.paths == ["http://127.0.0.1:11434/api/tags"]


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


def test_image_findings_require_submitted_frame_references():
    review_module = load_module("assisted_review_frame_refs_required", REVIEW_MODULE)
    finding = {"severity": "blocking", "claim": "visible condition", "evidenceFields": ["frameImages"]}
    with pytest.raises(review_module.ReviewError, match="frame"):
        review_module.validate_assessment(assessment("fail", [finding]), {"frameImages"})


@pytest.mark.parametrize("references", [[], ["0" * 64], ["a" * 64, "a" * 64]])
def test_image_findings_reject_unreviewed_or_duplicate_references(references):
    review_module = load_module("assisted_review_invalid_frame_refs", REVIEW_MODULE)
    finding = {"severity": "blocking", "claim": "visible condition", "evidenceFields": ["frameImages"], "frameRefs": references}
    with pytest.raises(review_module.ReviewError, match="frame"):
        review_module.validate_assessment(assessment("fail", [finding]), {"frameImages"}, {"a" * 64})


def test_frame_identity_is_bound_to_run_and_submitted_image(tmp_path):
    review_module = load_module("assisted_review_frame_run_binding", REVIEW_MODULE)
    run_fixture = load_module("assisted_run_frame_run_binding", REPOSITORY / "scripts" / "tests" / "test_assisted_benchmark_run.py")
    directory = tmp_path / "run-0001"
    capture = run_fixture.make_capture(directory)
    auth = authorization(("frameImages",))
    selected = review_module.select_evidence(capture, auth, contract(), directory)
    first = selected["frameImages"]["frames"][0]
    other_capture = {**capture, "runId": "run-other"}
    other_auth = {**auth, "runId": "run-other"}
    second = review_module.select_evidence(other_capture, other_auth, contract(), directory)["frameImages"]["frames"][0]
    assert first["frameId"] != second["frameId"]
    assert first["submittedSha256"] == second["submittedSha256"]
    finding = {"severity": "blocking", "claim": "foreign run", "evidenceFields": ["frameImages"], "frameRefs": [first["frameId"]]}
    with pytest.raises(review_module.ReviewError, match="frame"):
        review_module.validate_assessment(assessment("fail", [finding]), {"frameImages"}, {second["frameId"]})
    capture["frames"][0]["sha256"] = "0" * 64
    transport = FakeTransport()
    with pytest.raises(review_module.ReviewError, match="verified hash"):
        review_module.review_evidence(capture, auth, contract(), transport, directory)
    assert transport.payloads == []


def test_sampling_reports_unreviewed_transient_and_unknown_time(tmp_path):
    from PIL import Image

    review_module = load_module("assisted_review_transient_coverage", REVIEW_MODULE)
    run_fixture = load_module("assisted_run_transient_coverage", REPOSITORY / "scripts" / "tests" / "test_assisted_benchmark_run.py")
    directory = tmp_path / "run-0001"
    capture = run_fixture.make_capture(directory)
    source_frame = capture["frames"][0]
    default_bytes = (directory / source_frame["frameFile"]).read_bytes()
    event = io.BytesIO()
    Image.new("RGB", (1, 1), "red").save(event, format="PNG")
    frames = []
    for index in range(5):
        data = event.getvalue() if index == 1 else default_bytes
        filename = f"frame-{index + 1:05d}.png"
        (directory / filename).write_bytes(data)
        frames.append({**source_frame, "index": index, "tMs": None if index == 2 else index * 1000,
                       "sha256": hashlib.sha256(data).hexdigest(), "frameFile": filename})
    capture.update({"frames": frames, "frameCount": 5, "durationMs": 4000})
    finding = {"severity": "info", "claim": "event absent across entire capture", "evidenceFields": ["frameImages"]}
    transport = FakeTransport(response=assessment_response(content=assessment(findings=[finding])))
    result = review_module.review_evidence(capture, authorization(("frameImages",)), contract(), transport, directory)
    sampling = result["sampling"]
    assert sampling["coverageScope"] == "submitted-frames-only"
    assert sampling["availableFrameCount"] == 5
    assert sampling["unreviewedFrameCount"] == 2
    assert sampling["allManifestFramesReviewed"] is False
    assert sampling["unknownTimestampCount"] == 1
    assert sampling["knownTimeRangeMs"] == [0, 4000]
    assert [frame["index"] for frame in sampling["frames"]] == [0, 2, 4]
    assert sampling["frames"][1]["tMs"] is None
    assert "entire capture" not in json.dumps(result)
    ids = {frame["frameId"] for frame in sampling["frames"]}
    assert len(result["assessment"]["findings"]) == 3
    assert {finding["frameRefs"][0] for finding in result["assessment"]["findings"]} == ids
    for payload in transport.payloads:
        frame_id = json.loads(payload["messages"][1]["content"])["frameImages"]["frames"][0]["frameId"]
        assert payload["format"]["properties"]["findings"]["items"]["properties"]["frameRefs"]["items"]["enum"] == [frame_id]


def test_aggregation_preserves_a_blocker_after_more_than_32_earlier_findings(tmp_path):
    review_module = load_module("assisted_review_late_blocker", REVIEW_MODULE)
    run_fixture = load_module("assisted_run_late_blocker", REPOSITORY / "scripts" / "tests" / "test_assisted_benchmark_run.py")
    capture_directory = tmp_path / "run-0001"
    capture = run_fixture.make_capture(capture_directory)
    source_frame = capture["frames"][0]
    frame_bytes = (capture_directory / source_frame["frameFile"]).read_bytes()
    frames = []
    for index in range(3):
        filename = f"frame-{index + 1:05d}.png"
        (capture_directory / filename).write_bytes(frame_bytes)
        frames.append({**source_frame, "index": index, "tMs": index * 1000, "frameFile": filename})
    capture["frames"] = frames
    capture["frameCount"] = 3
    earlier = [
        {"severity": "info", "claim": f"earlier observation {index}", "evidenceFields": ["frameImages"]}
        for index in range(33)
    ]
    blocker = {"severity": "blocking", "claim": "later blocking condition", "evidenceFields": ["frameImages"]}
    responses = [assessment(findings=earlier[:32]), assessment(findings=earlier[32:]), assessment("fail", [blocker])]

    class SequentialTransport(FakeTransport):
        def chat(self, payload):
            response = copy.deepcopy(responses[len(self.payloads)])
            frame_id = json.loads(payload["messages"][1]["content"])["frameImages"]["frames"][0]["frameId"]
            for finding in response["findings"]:
                finding["frameRefs"] = [frame_id]
            self.payloads.append(payload)
            return assessment_response(content=response)

    result = review_module.review_evidence(
        capture, authorization(("frameImages",)), contract(), SequentialTransport(), capture_directory
    )
    assert result["assessment"]["decision"] == "fail"
    assert any(finding["severity"] == "blocking" for finding in result["assessment"]["findings"])
    blocker_identity = {**blocker, "frameRefs": result["assessment"]["findings"][0]["frameRefs"]}
    assert hashlib.sha256(review_module._json_bytes(blocker_identity)).hexdigest() in result["findingSha256"]
    review_module.validate_assessment(result["assessment"], {"frameImages"}, {frame["frameId"] for frame in result["sampling"]["frames"]})
    assert result["aggregation"]["omittedNonBlockingFindingCount"] == 2
    assert result["aggregation"]["completedInputCount"] == 3


@pytest.mark.parametrize("decisions, expected", [
    (["pass"], "pass"), (["pass", "pass"], "pass"),
    (["fail", "pass"], "fail"), (["pass", "fail"], "fail"),
    (["fail", "inconclusive"], "fail"), (["inconclusive", "fail"], "fail"),
    (["inconclusive"], "inconclusive"), (["pass", "inconclusive"], "inconclusive"),
])
def test_aggregation_preserves_decision_precedence(decisions, expected):
    review_module = load_module("assisted_review_decision_precedence", REVIEW_MODULE)
    blocker = {"severity": "blocking", "claim": "blocked", "evidenceFields": ["captureMetadata"]}
    inputs = [assessment(decision, [blocker] if decision == "fail" else []) for decision in decisions]
    result, summary = review_module._aggregate_assessments(inputs, {"captureMetadata"}, len(inputs))
    assert result["decision"] == expected
    assert summary["requiredInputCount"] == summary["completedInputCount"] == len(inputs)
    review_module.validate_assessment(result, {"captureMetadata"})


@pytest.mark.parametrize("inputs, required_count", [
    ([], 0), ([], 1), ([assessment()], 2), ([assessment(), assessment()], 1),
    ([assessment()], True), ([assessment()], -1), ([assessment()], 1.0),
    ([None], 1), ([{"decision": "pass"}], 1),
    ([assessment("fail")], 1),
    ([assessment("pass", [{"severity": "blocking", "claim": "blocked", "evidenceFields": ["captureMetadata"]}])], 1),
])
def test_aggregation_refuses_incomplete_or_contradictory_inputs(inputs, required_count):
    review_module = load_module("assisted_review_incomplete_aggregate", REVIEW_MODULE)
    with pytest.raises(review_module.ReviewError):
        review_module._aggregate_assessments(inputs, {"captureMetadata"}, required_count)


def test_aggregation_refuses_blockers_that_exceed_the_output_bound():
    review_module = load_module("assisted_review_blocker_overflow", REVIEW_MODULE)
    blockers = [
        {"severity": "blocking", "claim": f"blocker {index}", "evidenceFields": ["captureMetadata"]}
        for index in range(33)
    ]
    with pytest.raises(review_module.ReviewError, match="incomplete.*blocking findings"):
        review_module._aggregate_assessments(
            [assessment("fail", blockers[:32]), assessment("fail", blockers[32:])], {"captureMetadata"}, 2
        )


def test_aggregation_retains_all_32_blockers_and_reports_optional_omissions():
    review_module = load_module("assisted_review_blocker_boundary", REVIEW_MODULE)
    blockers = [
        {"severity": "blocking", "claim": f"blocker {index}", "evidenceFields": ["captureMetadata"]}
        for index in range(32)
    ]
    optional = [
        {"severity": "info", "claim": f"observation {index}", "evidenceFields": ["captureMetadata"]}
        for index in range(64)
    ]
    inputs = [assessment(findings=optional[:32]), assessment(findings=optional[32:]), assessment("fail", blockers)]
    result, summary = review_module._aggregate_assessments(inputs, {"captureMetadata"}, 3)
    assert result["decision"] == "fail"
    assert result["findings"] == blockers
    assert summary["omittedNonBlockingFindingCount"] == 64


def test_aggregation_uses_stable_severity_order_and_full_evidence_identity():
    review_module = load_module("assisted_review_deduplication", REVIEW_MODULE)
    info = {"severity": "info", "claim": "same observation", "evidenceFields": ["captureMetadata"]}
    different_support = {**info, "evidenceFields": ["resourceSamples"]}
    combined_support = {**info, "evidenceFields": ["resourceSamples", "captureMetadata"]}
    warning = {"severity": "warning", "claim": "warning", "evidenceFields": ["captureMetadata"]}
    blocker = {"severity": "blocking", "claim": "blocked", "evidenceFields": ["captureMetadata"]}
    inputs = [
        assessment(findings=[info, different_support, combined_support, warning]),
        assessment("fail", [blocker, info, {**combined_support, "evidenceFields": list(reversed(combined_support["evidenceFields"]))}]),
    ]
    original = copy.deepcopy(inputs)
    result, summary = review_module._aggregate_assessments(inputs, {"captureMetadata", "resourceSamples"}, 2)
    assert result["findings"] == [blocker, warning, info, different_support,
                                   {**combined_support, "evidenceFields": ["captureMetadata", "resourceSamples"]}]
    assert inputs == original
    assert review_module._aggregate_assessments(inputs, {"resourceSamples", "captureMetadata"}, 2) == (result, summary)


def test_aggregation_does_not_pass_when_uncertainties_are_present():
    review_module = load_module("assisted_review_uncertain_aggregate", REVIEW_MODULE)
    inputs = [assessment(), assessment()]
    inputs[0]["uncertainties"] = [f"unknown {index}" for index in range(32)]
    inputs[1]["uncertainties"] = ["unknown 0", "unknown 32"]
    inputs[1]["recommendation"] = "inspect the unresolved condition"
    result, summary = review_module._aggregate_assessments(inputs, {"captureMetadata"}, 2)
    assert result["decision"] == "inconclusive"
    assert result["uncertainties"] == inputs[0]["uncertainties"]
    assert summary["omittedUncertaintyCount"] == 1
    assert summary["omittedRecommendationCount"] == 2


def test_aggregation_revalidates_its_final_schema_and_decision(monkeypatch):
    review_module = load_module("assisted_review_final_validation", REVIEW_MODULE)
    original = review_module.validate_assessment
    calls = []

    def record_validation(value, approved_fields, reviewed_frames=None):
        calls.append(copy.deepcopy(value))
        return original(value, approved_fields, reviewed_frames)

    monkeypatch.setattr(review_module, "validate_assessment", record_validation)
    result, _summary = review_module._aggregate_assessments([assessment(), assessment()], {"captureMetadata"}, 2)
    assert len(calls) == 3
    assert calls[-1] == result


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


def test_images_require_explicit_disclosure_beyond_field_approval(tmp_path):
    review_module = load_module("assisted_review_disclosure_required", REVIEW_MODULE)
    run_fixture = load_module("assisted_run_disclosure_fixture", REPOSITORY / "scripts" / "tests" / "test_assisted_benchmark_run.py")
    capture_directory = tmp_path / "run-0001"
    capture = run_fixture.make_capture(capture_directory)
    auth = authorization(("frameImages",))
    auth.pop("reviewPrivacy", None)
    transport = FakeTransport()
    with pytest.raises(review_module.ReviewError, match="privacy"):
        review_module.review_evidence(capture, auth, contract(), transport, capture_directory)
    assert transport.payloads == []


def test_regions_strip_unapproved_pixels_metadata_and_model_prose(tmp_path):
    from PIL import Image, ImageDraw, PngImagePlugin

    review_module = load_module("assisted_review_privacy_canaries", REVIEW_MODULE)
    run_fixture = load_module("assisted_run_privacy_canaries", REPOSITORY / "scripts" / "tests" / "test_assisted_benchmark_run.py")
    capture_directory = tmp_path / "run-0001"
    capture = run_fixture.make_capture(capture_directory)
    canary = "PRIVATE_CANARY_8471"
    image = Image.new("RGB", (120, 40), "white")
    drawing = ImageDraw.Draw(image)
    drawing.rectangle((60, 0, 119, 39), fill="blue")
    drawing.text((64, 4), canary, fill="white")
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("private-metadata", canary)
    source = io.BytesIO()
    image.save(source, format="PNG", pnginfo=metadata)
    original = source.getvalue()
    frame = capture["frames"][0]
    (capture_directory / frame["frameFile"]).write_bytes(original)
    frame.update({"width": 120, "height": 40, "sha256": hashlib.sha256(original).hexdigest()})
    auth = authorization(("frameImages",))
    auth["reviewPrivacy"].update({"imageMode": "regions", "frames": [
        {"index": 0, "crop": [0, 0, 60, 40], "redactions": [[0, 0, 10, 10]]}
    ]})
    finding = {"severity": "blocking", "claim": canary, "evidenceFields": ["frameImages"]}
    raw_assessment = assessment("fail", [finding])
    raw_assessment["recommendation"] = canary
    raw_assessment["uncertainties"] = [canary]
    transport = FakeTransport(response=assessment_response(content=raw_assessment))
    result = review_module.review_evidence(capture, auth, contract(), transport, capture_directory)
    submitted = review_module.base64.b64decode(transport.payload["messages"][1]["images"][0])
    assert canary.encode() not in submitted
    with Image.open(io.BytesIO(submitted)) as transformed:
        assert transformed.size == (60, 40)
        assert transformed.getpixel((0, 0)) == (0, 0, 0)
        assert transformed.getpixel((20, 20)) == (255, 255, 255)
        assert transformed.info == {}
    assert (capture_directory / frame["frameFile"]).read_bytes() == original
    assert canary not in json.dumps(result)
    prompt = json.loads(transport.payload["messages"][1]["content"])
    assert prompt["frameImages"]["frames"][0]["submittedSha256"] == hashlib.sha256(submitted).hexdigest()
    assert result["assessment"]["decision"] == "fail"
    again = FakeTransport(response=assessment_response(content=raw_assessment))
    review_module.review_evidence(capture, auth, contract(), again, capture_directory)
    assert again.payloads == transport.payloads


@pytest.mark.parametrize("problem", ["expired", "text-retention", "unknown-mode", "invalid-box", "outside-box", "missing-region"])
def test_invalid_privacy_never_submits_images(tmp_path, problem):
    review_module = load_module("assisted_review_bad_privacy", REVIEW_MODULE)
    run_fixture = load_module("assisted_run_bad_privacy", REPOSITORY / "scripts" / "tests" / "test_assisted_benchmark_run.py")
    capture_directory = tmp_path / "run-0001"
    capture = run_fixture.make_capture(capture_directory)
    auth = authorization(("frameImages",))
    policy = auth["reviewPrivacy"]
    if problem == "expired":
        policy["expiresAt"] = "2000-01-01T00:00:00Z"
    elif problem == "text-retention":
        policy["retainModelText"] = True
    elif problem == "unknown-mode":
        policy["imageMode"] = "automatic"
    else:
        policy.update({"imageMode": "regions", "frames": [{"index": 0, "crop": [0, 0, 1, 1], "redactions": []}]})
        if problem == "invalid-box":
            policy["frames"][0]["crop"][2] = 0
        elif problem == "outside-box":
            policy["frames"][0]["crop"][2] = 100
        else:
            policy["frames"][0]["index"] = 3
    transport = FakeTransport()
    with pytest.raises((review_module.ReviewError, review_module.ContractError), match="privacy"):
        review_module.review_evidence(capture, auth, contract(), transport, capture_directory)
    assert transport.payloads == []


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