from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import struct
import sys
import zlib
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
CONTRACT_PATH = REPOSITORY / "tools" / "assisted-benchmark" / "lba-mcp-contract.json"
SCRIPTS = REPOSITORY / "scripts"
VM_UUID = "afc966a5-f120-4d3e-9760-69fd6344a03f"
ACTOR_VERSION = "1.4.14"
ACTOR_REVISION = "7f65a2215b952e1f31bc3677087f14c8b528d789"
MODEL = {
    "tag": "projectbuddy:0.1-qwen2.5-7b",
    "digest": "845dbda0ea48ed749caafd9e6037047aa19acfcfd82e704d7ca97d631a0b697e",
}

sys.path.insert(0, str(SCRIPTS))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load test module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def get_contract():
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def authorization():
    return {
        "schema": "projectbuddy/assisted-benchmark-authorization@1",
        "runId": "run-0001",
        "workload": "labview-launch",
        "sourceRevision": ACTOR_REVISION,
        "approvedBy": "operator",
        "approvedAt": "2026-11-10T12:00:00Z",
        "approvedCalls": [
            {"tool": "get_host_capabilities", "arguments": {}},
            {"tool": "get_benchmark_series", "arguments": {}},
        ],
        "targetVmUuids": [],
        "ollamaModel": dict(MODEL),
        "evidenceFields": ["captureMetadata", "frameHashes", "resourceSamples", "benchmarkResult"],
            "evidenceFields": ["captureMetadata", "frameHashes", "resourceSamples", "frameImages", "benchmarkResult"],
        "allowVmLifecycle": False,
        "teardownOnNativePplFailure": False,
    }


def make_capture(root: Path):
    module = load_module("capture_fixture_for_run", SCRIPTS / "assisted_benchmark_capture.py")
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
        "0000000d49444154789c63606060f80f0001040100a899cd1d0000000049454e44ae426082"
    )
    root.mkdir()
    (root / "frame-00001.png").write_bytes(png)
    (root / "capture-meta.json").write_text(json.dumps({
        "workload": "labview-launch", "plane": "LINUX", "source": "ffmpeg-x11grab",
    }), encoding="utf-8")
    (root / "capture-status.json").write_text(json.dumps({
        "schema": "labview-benchmark-actor/capture-status@1", "state": "stopped",
        "runDir": str(root.resolve()), "startedAt": "2026-11-10T12:00:00Z",
        "stoppedAt": "2026-11-10T12:00:01Z", "frameCount": 1,
        "durationMs": 0, "samples": 1, "captureJsonReady": True,
    }), encoding="utf-8")
    (root / "capture.json").write_text(json.dumps({
        "schema": "labview-benchmark-actor/launch-capture@1", "workload": "labview-launch",
        "plane": "LINUX", "source": "ffmpeg-x11grab", "frameCount": 1, "durationMs": 0,
        "screen": None, "frames": [{"index": 0, "tMs": 0, "image": "frame-00001.png", "imageBytes": len(png), "dhashHex": None}],
    }), encoding="utf-8")
    (root / "resources.jsonl").write_text(json.dumps({"ms": 1, "cpuPct": 20, "ramMb": 1024, "diskPct": 2}) + "\n", encoding="utf-8")
    evidence = module.validate_capture(root, ACTOR_VERSION, ACTOR_REVISION)
    return evidence


def ppl_proof():
    source = {"repo": "ni/labview-icon-editor", "repoHead": "9545c483f2b947c71de68c7f70aedefaedadabf7", "project": "lv_icon_editor.lvproj"}
    build = {"target": "My Computer", "buildSpec": "Editor Packed Library"}
    result_identity = {
        "project": source["project"], "target": build["target"],
        "buildSpec": build["buildSpec"], "generatedArtifact": "lv_icon.lvlibp",
        "operationSucceeded": True,
    }
    result_hash = hashlib.sha256(json.dumps(result_identity, separators=(",", ":")).encode()).hexdigest()
    proof = {
        "schema": "labview-benchmark-actor/ppl-build-benchmark@1",
        "plane": "linux-container", "source": source,
        "operation": "LabVIEWCLI -OperationName ExecuteBuildSpec", "build": build,
        "generatedArtifact": "lv_icon.lvlibp", "operationSucceeded": True,
        "timing": {"buildSeconds": 59}, "artifactSizeBytes": 2948583,
        "resultHash": result_hash,
        "verdict": {"benchmarkOk": True, "reason": "verified fixture"},
    }
    digest_identity = {
        "schema": proof["schema"], "project": source["project"],
        "target": build["target"], "buildSpec": build["buildSpec"],
        "generatedArtifact": "lv_icon.lvlibp", "operationSucceeded": True,
        "resultHash": result_hash, "verdict": {"benchmarkOk": True},
    }
    proof["digest"] = hashlib.sha256(json.dumps(digest_identity, separators=(",", ":")).encode()).hexdigest()
    return proof


def mcp_snapshot(contract):
    tools = []
    for group in contract["toolGroups"].values():
        tools.extend({"name": item["name"], "description": "fixture", "inputSchema": copy.deepcopy(item["inputSchema"])} for item in group["tools"])
    return {
        "protocolVersion": contract["source"]["protocolVersion"],
        "serverInfo": {"name": contract["source"]["serverName"], "version": contract["source"]["serverVersion"]},
        "tools": tools,
    }


class FakeMcp:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.calls = []

    def initialize_and_list_tools(self):
        return self.snapshot

    def call(self, tool, arguments):
        self.calls.append((tool, arguments))
        return {"content": [{"type": "text", "text": json.dumps({"result": tool})}]}


class FakeOllama:
    def __init__(self):
        self.tag_calls = 0
        self.payload = None

    def tags(self):
        self.tag_calls += 1
        return {"models": [{"name": MODEL["tag"], "digest": MODEL["digest"]}]}

    def show(self, model):
        return {"capabilities": ["completion", "vision"]}

    def chat(self, payload):
        self.payload = payload
        return {
            "model": MODEL["tag"], "done": True, "done_reason": "stop",
            "message": {"content": json.dumps({
                "decision": "pass", "findings": [], "uncertainties": [],
                "recommendation": "retain the verified receipts",
            })},
        }


def test_orchestrator_binds_mcp_capture_model_and_ppl_receipts(tmp_path: Path):
    module = load_module("assisted_benchmark_run_test", SCRIPTS / "assisted_benchmark_run.py")
    contract = get_contract()
    capture = make_capture(tmp_path / "run-0001")
    mcp = FakeMcp(mcp_snapshot(contract))
    ollama = FakeOllama()

    receipt = module.orchestrate(
        authorization(), contract, capture, ppl_proof(), mcp, ollama, tmp_path / "run-0001"
    )

    assert receipt["schema"] == "projectbuddy/assisted-benchmark-run@1"
    assert receipt["runId"] == "run-0001"
    assert receipt["pplProof"]["artifact"] == "lv_icon.lvlibp"
    assert receipt["approvedReviewModel"] == MODEL
    assert receipt["reviewModel"] == MODEL
    assert receipt["reviewAggregation"] == {
        "policyVersion": "projectbuddy/assessment-aggregation@1",
        "requiredInputCount": 1,
        "completedInputCount": 1,
        "omittedNonBlockingFindingCount": 0,
        "omittedUncertaintyCount": 0,
        "omittedRecommendationCount": 0,
    }
    assert [item["tool"] for item in receipt["mcp"]["calls"]] == ["get_host_capabilities", "get_benchmark_series"]
    assert all("resultSha256" in item and "text" not in item for item in receipt["mcp"]["calls"])
    assert receipt["assessment"]["decision"] == "pass"
    assert receipt["reviewModel"]["tag"] == MODEL["tag"]
    assert receipt["reviewedFrameCount"] == 1
    assert receipt["advisoryOnly"] is True
    assert receipt["receiptSha256"] == hashlib.sha256(module.canonical_json({key: value for key, value in receipt.items() if key != "receiptSha256"})).hexdigest()
    assert ollama.tag_calls == 2
    assert str(tmp_path) not in json.dumps(receipt)


def test_orchestrator_rejects_bad_ppl_proof_before_mcp_calls(tmp_path: Path):
    module = load_module("assisted_benchmark_run_bad_proof", SCRIPTS / "assisted_benchmark_run.py")
    contract = get_contract()
    capture = make_capture(tmp_path / "run-0001")
    mcp = FakeMcp(mcp_snapshot(contract))
    proof = ppl_proof()
    proof["generatedArtifact"] = "dependencies.lvlibp"
    with pytest.raises(module.RunError, match="approved lv_icon.lvlibp"):
        module.orchestrate(authorization(), contract, capture, proof, mcp, FakeOllama(), tmp_path / "run-0001")
    assert mcp.calls == []


def test_orchestrator_rejects_capture_and_authorization_run_mismatch(tmp_path: Path):
    module = load_module("assisted_benchmark_run_run_mismatch", SCRIPTS / "assisted_benchmark_run.py")
    contract = get_contract()
    capture = make_capture(tmp_path / "run-0001")
    auth = authorization()
    auth["runId"] = "other-run"
    mcp = FakeMcp(mcp_snapshot(contract))
    with pytest.raises(module.RunError, match="does not match"):
        module.orchestrate(auth, contract, capture, ppl_proof(), mcp, FakeOllama(), tmp_path / "run-0001")
    assert mcp.calls == []


def test_stdio_mcp_client_performs_handshake_and_serial_calls():
    module = load_module("assisted_benchmark_run_stdio", SCRIPTS / "assisted_benchmark_run.py")
    contract = get_contract()
    snapshot = mcp_snapshot(contract)
    server = r'''import json,sys
snapshot = ''' + repr(snapshot) + r'''
for line in sys.stdin:
    msg=json.loads(line)
    method=msg.get("method")
    if method == "notifications/initialized":
        continue
    if method == "initialize":
        result={"protocolVersion":snapshot["protocolVersion"],"serverInfo":snapshot["serverInfo"],"capabilities":{"tools":{}}}
    elif method == "tools/list":
        result={"tools":snapshot["tools"]}
    elif method == "tools/call":
        result={"content":[{"type":"text","text":msg["params"]["name"]}]}
    else:
        result={}
    print(json.dumps({"jsonrpc":"2.0","id":msg["id"],"result":result}), flush=True)
'''
    with module.McpStdioClient([sys.executable, "-u", "-c", server]) as client:
        discovered = client.initialize_and_list_tools()
        listed = module.validate_snapshot(discovered, contract)
        result = client.call("get_benchmark_series", {})
    assert listed["toolCount"] == 13
    assert result["content"][0]["text"] == "get_benchmark_series"


def test_receipt_writer_is_private_atomic_and_does_not_overwrite(tmp_path: Path):
    module = load_module("assisted_benchmark_run_receipt_writer", SCRIPTS / "assisted_benchmark_run.py")
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    private.chmod(0o700)
    output = private / "receipt.json"
    receipt = {"schema": "projectbuddy/assisted-benchmark-run@1", "runId": "run-0001"}

    module.write_private_receipt(output, receipt)

    assert output.read_text(encoding="utf-8") == json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n"
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(module.RunError, match="new absolute file path"):
        module.write_private_receipt(output, receipt)


def test_receipt_writer_refuses_a_public_directory(tmp_path: Path):
    module = load_module("assisted_benchmark_run_public_receipt", SCRIPTS / "assisted_benchmark_run.py")
    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    public.chmod(0o755)
    with pytest.raises(module.RunError, match="must be private"):
        module.write_private_receipt(public / "receipt.json", {"runId": "run-0001"})