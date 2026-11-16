"""Admit bounded active-capture MCP evidence without controlling capture."""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import io
import json
import math
import re
import time
from datetime import datetime
from pathlib import Path
from uuid import UUID

from PIL import Image

from assisted_benchmark_contract import SERVER_SET_SCHEMA, load_contract, tool_contract_for_call, validate_authorization, validate_contract, validate_snapshot
from assisted_benchmark_live_policy import sha256
from assisted_benchmark_capture import ACTOR_VERSION

SESSION_SCHEMA = "labview-benchmark-actor/active-capture-session@1"
BATCH_SCHEMA = "active-capture-evidence-batch@1"
MAX_FRAME_BYTES = 8 * 1024 * 1024
STATES = {"capturing": 0, "finalizing": 1, "stopped": 2, "failed": 2}
RESOURCE_CONTRACT = {
    "schema": "projectbuddy/active-capture-mcp-resources@1",
    "sessionSchema": SESSION_SCHEMA,
    "batchSchema": BATCH_SCHEMA,
    "sessionUriTemplate": "lba-capture://session/{runId}",
    "maxBatchSize": 12,
    "maxFrameBytes": MAX_FRAME_BYTES,
}
DEFAULT_ACTIVE_PIN = Path(__file__).resolve().parents[1] / "tools" / "assisted-benchmark" / "active-capture-pin.json"


class ActiveCaptureError(ValueError):
    """Evidence cannot be admitted under the sealed capture binding."""


class McpResourceClient:
    def __init__(self, session):
        _require(callable(getattr(session, "read_resource", None)))
        self._session = session

    async def read_resource(self, uri: str) -> dict:
        result = await self._session.read_resource(uri)
        return result.model_dump(mode="json", by_alias=True, exclude_none=True, exclude_unset=True)


def _require(condition: bool) -> None:
    if not condition:
        raise ActiveCaptureError("active capture evidence violates the sealed contract")


def _integer(value: object, minimum: int = 0) -> int:
    _require(type(value) is int and minimum <= value <= 1_000_000_000)
    return value


def _timestamp(value: object) -> datetime:
    _require(isinstance(value, str) and len(value) <= 40)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        _require(parsed.tzinfo is not None)
        return parsed
    except ValueError:
        raise ActiveCaptureError("active capture timestamp is invalid") from None


def _uuid(value: object) -> None:
    try:
        _require(isinstance(value, str) and str(UUID(value)) == value)
    except (ValueError, AttributeError):
        raise ActiveCaptureError("active capture identity is invalid") from None


def load_active_capture_contract(pin_path: Path = DEFAULT_ACTIVE_PIN) -> dict:
    base = load_contract()
    try:
        pin = json.loads(pin_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ActiveCaptureError("local capture pin is unavailable or malformed") from None
    _require(isinstance(pin, dict) and set(pin) == {"schema", "activation", "baseContractSha256", "source", "captureResources"})
    _require(pin["schema"] == "projectbuddy/active-capture-pin@1" and pin["activation"] == "local-candidate-only")
    _require(pin["baseContractSha256"] == sha256(base) and pin["captureResources"] == RESOURCE_CONTRACT)
    source = pin["source"]
    _require(isinstance(source, dict) and set(source) == set(base["source"]))
    _require(all(source[key] == base["source"][key] for key in source if key not in {"revision", "serverVersion"}))
    _require(isinstance(source["revision"], str) and re.fullmatch(r"[0-9a-f]{40}", source["revision"]) is not None)
    _require(isinstance(source["serverVersion"], str) and ACTOR_VERSION.fullmatch(source["serverVersion"]) is not None)
    candidate = {**base, "source": copy.deepcopy(source), "captureResources": copy.deepcopy(pin["captureResources"])}
    validate_contract(candidate)
    return candidate


class ActiveCaptureConsumer:
    def __init__(self, contract: dict, snapshot: dict, authorization: dict, binding: dict, client,
                 *, max_reads: int = 128, max_frames: int = 128, total_seconds: float = 120,
                 request_seconds: float = 10, clock=time.monotonic, actor_server_id: str | None = None,
                 capture_workload: str = "labview-launch"):
        grant = validate_authorization(authorization, contract)
        actor, _tool = tool_contract_for_call(contract, "get_host_capabilities", actor_server_id)
        validate_snapshot(snapshot, actor)
        revision = contract["sourceRevision"] if contract["schema"] == SERVER_SET_SCHEMA else actor["source"]["revision"]
        _require(grant["sourceRevision"] == revision)
        _require(actor.get("captureResources") == RESOURCE_CONTRACT)
        _require(capture_workload in {"labview-launch", "desktop-capture"})
        self._capture_workload = capture_workload
        _require(isinstance(binding, dict) and set(binding) == {
            "schema", "grantRunId", "vmUuid", "captureRunId", "actorSource", "contractSha256",
        })
        _require(binding["schema"] == "projectbuddy/active-capture-binding@1")
        _require(binding["grantRunId"] == grant["runId"] and binding["vmUuid"] in grant["targetVmUuids"])
        _require(binding["actorSource"] == actor["source"] and binding["contractSha256"] == sha256(actor))
        _require({"captureMetadata", "frameHashes", "frameImages", "resourceSamples"}.issubset(grant["evidenceFields"]))
        _uuid(binding["captureRunId"])
        _uuid(binding["vmUuid"])
        _require(type(max_reads) is int and 1 <= max_reads <= 128)
        _require(type(max_frames) is int and 1 <= max_frames <= 1536)
        _require(all(type(value) in {int, float} and math.isfinite(value) for value in (total_seconds, request_seconds)))
        _require(0 < request_seconds <= total_seconds <= 900)
        _require(callable(getattr(client, "read_resource", None)))
        self._binding = copy.deepcopy(binding)
        self._client = client
        self._uri = f"lba-capture://session/{binding['captureRunId']}"
        self._reads = 0
        self._max_reads = max_reads
        self._max_frames = max_frames
        self._clock = clock
        self._deadline = clock() + total_seconds
        self._request_seconds = request_seconds
        self._lock = asyncio.Lock()
        self._incomplete = False
        self._frame_cursor = 0
        self._sample_cursor = 0
        self._watermarks = {"frame": 0, "sample": 0}
        self._frame_time = None
        self._sample_time = None
        self._session = None
        self._observed_state = None
        self._drained = False
        self._frames = {}
        self._verified = set()

    @property
    def final(self) -> bool:
        return bool(not self._incomplete and self._session and self._session["state"] == "stopped"
                    and self._drained and self._frame_cursor > 0 and self._sample_cursor > 0
                    and len(self._verified) == len(self._frames))

    @property
    def incomplete(self) -> bool:
        return self._incomplete

    @property
    def binding(self) -> dict:
        return copy.deepcopy(self._binding)

    async def _read(self, uri: str, mime_type: str) -> dict:
        _require(not self._incomplete)
        remaining = self._deadline - self._clock()
        _require(remaining > 0 and self._reads < self._max_reads)
        self._reads += 1
        timeout = min(remaining, self._request_seconds)
        request_deadline = self._clock() + timeout
        result = await asyncio.wait_for(self._client.read_resource(uri), timeout)
        _require(self._clock() < min(request_deadline, self._deadline))
        _require(isinstance(result, dict) and set(result) == {"contents"})
        contents = result["contents"]
        _require(isinstance(contents, list) and len(contents) == 1 and isinstance(contents[0], dict))
        content = contents[0]
        payload = "text" if mime_type == "application/json" else "blob"
        _require(set(content) == {"uri", "mimeType", payload} and content["uri"] == uri and content["mimeType"] == mime_type)
        _require(isinstance(content[payload], str) and len(content[payload]) <= (128 * 1024 if payload == "text" else 4 * ((MAX_FRAME_BYTES + 2) // 3)))
        return content

    async def _json(self, uri: str) -> dict:
        content = await self._read(uri, "application/json")
        def closed_pairs(pairs):
            result = {}
            for key, value in pairs:
                _require(key not in result)
                result[key] = value
            return result
        value = json.loads(content["text"], object_pairs_hook=closed_pairs)
        _require(isinstance(value, dict))
        return value

    def _lifecycle(self, state: object) -> None:
        _require(isinstance(state, str) and state in STATES)
        if self._observed_state:
            previous = self._observed_state
            _require(STATES[state] >= STATES[previous] and (previous not in {"stopped", "failed"} or previous == state))

    def _validate_session(self, session: dict) -> None:
        _require(set(session) == {"schema", "runId", "workload", "state", "plane", "source", "startedAt",
                                  "stoppedAt", "availableEvidence", "updatedAt", "watermarks", "incomplete"})
        _require(session["schema"] == SESSION_SCHEMA and session["runId"] == self._binding["captureRunId"])
        _require(session["workload"] == self._capture_workload and session["plane"] in {"WIN", "LINUX"})
        _require(isinstance(session["source"], str) and re.fullmatch(r"[A-Za-z0-9._-]{1,80}", session["source"]) is not None)
        self._lifecycle(session["state"])
        started = _timestamp(session["startedAt"])
        _require(_timestamp(session["updatedAt"]) >= started)
        if session["state"] in {"stopped", "failed"}:
            _require(_timestamp(session["stoppedAt"]) >= started)
        else:
            _require(session["stoppedAt"] is None)
        _require(session["availableEvidence"] == ["frames", "resource-samples"] and type(session["incomplete"]) is bool)
        _require(isinstance(session["watermarks"], dict) and set(session["watermarks"]) == {"frameSequence", "sampleSequence"})
        _require(_integer(session["watermarks"]["frameSequence"]) >= self._watermarks["frame"])
        _require(_integer(session["watermarks"]["sampleSequence"]) >= self._watermarks["sample"])
        if self._session:
            _require(all(session[key] == self._session[key] for key in ("runId", "workload", "plane", "source", "startedAt")))
            _require(_timestamp(session["updatedAt"]) >= _timestamp(self._session["updatedAt"]))
            if self._session["state"] == "stopped":
                _require(session["watermarks"] == {"frameSequence": self._watermarks["frame"], "sampleSequence": self._watermarks["sample"]})

    async def poll(self, limit: int = 12) -> dict:
        async with self._lock:
            try:
                _require(type(limit) is int and 1 <= limit <= 12)
                session = await self._json(self._uri)
                self._validate_session(session)
                uri = f"{self._uri}/batch?afterFrame={self._frame_cursor}&afterSample={self._sample_cursor}&limit={limit}"
                batch = await self._json(uri)
                self._validate_batch(batch, session, limit)
                self._session = copy.deepcopy(session)
                self._observed_state = batch["state"]
                if session["incomplete"] or batch["incomplete"] or batch["state"] == "failed":
                    self._incomplete = True
                    return {"state": batch["state"], "incomplete": True, "final": False, "frames": [], "samples": []}
                _require(len(self._frames) + len(batch["frames"]) <= self._max_frames)
                for frame in batch["frames"]:
                    self._frames[frame["sequence"]] = copy.deepcopy(frame)
                    self._frame_time = _timestamp(frame["capturedAt"])
                if batch["samples"]:
                    self._sample_time = _timestamp(batch["samples"][-1]["capturedAt"])
                self._frame_cursor, self._sample_cursor = batch["next"]["frame"], batch["next"]["sample"]
                self._watermarks = copy.deepcopy(batch["watermarks"])
                self._drained = batch["next"] == batch["watermarks"]
                return {"state": batch["state"], "incomplete": False, "final": self.final,
                        "frames": copy.deepcopy(batch["frames"]), "samples": copy.deepcopy(batch["samples"])}
            except asyncio.CancelledError:
                self._incomplete = True
                raise
            except Exception:
                self._incomplete = True
                raise ActiveCaptureError("active capture admission failed; no retry is permitted") from None

    def _validate_batch(self, batch: dict, session: dict, limit: int) -> None:
        _require(set(batch) == {"schema", "runId", "state", "after", "next", "watermarks", "frames", "samples", "frameGaps",
                                "incompleteFrames", "incompleteSamples", "sampleTailIncomplete", "sampleLogUnavailable", "incomplete"})
        _require(batch["schema"] == BATCH_SCHEMA and batch["runId"] == self._binding["captureRunId"])
        self._lifecycle(batch["state"])
        _require(STATES.get(batch["state"], -1) >= STATES[session["state"]])
        _require(batch["after"] == {"frame": self._frame_cursor, "sample": self._sample_cursor})
        for key in ("after", "next", "watermarks"):
            _require(isinstance(batch[key], dict) and set(batch[key]) == {"frame", "sample"})
            for kind in ("frame", "sample"):
                _integer(batch[key][kind])
        for key in ("incomplete", "sampleTailIncomplete", "sampleLogUnavailable"):
            _require(type(batch[key]) is bool)
        for key in ("frameGaps", "incompleteFrames", "incompleteSamples"):
            _require(isinstance(batch[key], list) and len(batch[key]) <= 12)
        marked = bool(batch["frameGaps"] or batch["incompleteFrames"] or batch["incompleteSamples"]
                      or batch["sampleTailIncomplete"] or batch["sampleLogUnavailable"])
        _require(batch["incomplete"] == marked)
        for kind, records_key, cursor, last_time in (("frame", "frames", self._frame_cursor, self._frame_time),
                                                     ("sample", "samples", self._sample_cursor, self._sample_time)):
            records = batch[records_key]
            _require(isinstance(records, list) and len(records) <= limit)
            _require(batch["watermarks"][kind] >= session["watermarks"]["frameSequence" if kind == "frame" else "sampleSequence"])
            expected = cursor
            for record in records:
                _require(isinstance(record, dict))
                sequence = _integer(record.get("sequence"), 1)
                _require(sequence > expected and (marked or sequence == expected + 1))
                timestamp = _timestamp(record.get("capturedAt"))
                _require(timestamp >= _timestamp(session["startedAt"]))
                _require(last_time is None or timestamp >= last_time)
                last_time, expected = timestamp, sequence
                if kind == "frame":
                    _require(set(record) == {"sequence", "capturedAt", "sizeBytes", "sha256", "uri"})
                    _require(_integer(record["sizeBytes"], 1) <= MAX_FRAME_BYTES)
                    _require(isinstance(record["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", record["sha256"]) is not None)
                    _require(record["uri"] == f"{self._uri}/frame/{sequence}?sha256={record['sha256']}")
                else:
                    _require(set(record) == {"sequence", "capturedAt", "cpuPct", "ramMb", "diskPct", "disks"})
                    _require(isinstance(record["disks"], list) and len(record["disks"]) <= 32)
                    values = [record[key] for key in ("cpuPct", "ramMb", "diskPct")]
                    for disk in record["disks"]:
                        _require(isinstance(disk, dict) and set(disk) == {"name", "writeMBs", "readMBs"})
                        _require(isinstance(disk["name"], str) and re.fullmatch(r"[A-Za-z0-9 _.-]{1,64}", disk["name"]) is not None)
                        values.extend((disk["writeMBs"], disk["readMBs"]))
                    _require(all(value is None or (type(value) in {int, float} and math.isfinite(value)) for value in values))
            _require(batch["next"][kind] == expected <= batch["watermarks"][kind])
            _require(marked or records or batch["watermarks"][kind] == cursor)

    async def read_frame(self, sequence: int) -> bytes:
        async with self._lock:
            try:
                _require(type(sequence) is int and sequence in self._frames and sequence not in self._verified)
                frame = self._frames[sequence]
                content = await self._read(frame["uri"], "image/png")
                data = base64.b64decode(content["blob"], validate=True)
                _require(len(data) == frame["sizeBytes"] and hashlib.sha256(data).hexdigest() == frame["sha256"])
                with Image.open(io.BytesIO(data)) as image:
                    _require(image.format == "PNG" and 0 < image.width <= 16384 and 0 < image.height <= 16384
                             and image.width * image.height <= 50_000_000)
                    image.verify()
                self._verified.add(sequence)
                return data
            except asyncio.CancelledError:
                self._incomplete = True
                raise
            except Exception:
                self._incomplete = True
                raise ActiveCaptureError("active capture frame verification failed; no retry is permitted") from None