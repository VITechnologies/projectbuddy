from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil

import pytest

from test_assisted_benchmark_capture import png
from test_assisted_benchmark_contract import REPOSITORY, VM_UUID
from test_assisted_benchmark_review import MODEL, authorization, contract
from test_assisted_benchmark_live_policy import fake_host_identity, live_choice_dispatcher

RUN_ID = "a0f4e148-02c2-46ac-a36d-dbdcb897187d"


@pytest.fixture
def rig(monkeypatch):
    monkeypatch.syspath_prepend(str(REPOSITORY / "scripts"))
    module = importlib.import_module("assisted_benchmark_session")
    capture_module = importlib.import_module("assisted_benchmark_active_capture")
    pinned = contract()
    pinned["captureResources"] = copy.deepcopy(capture_module.RESOURCE_CONTRACT)
    grant = authorization(fields=("captureMetadata", "resourceSamples", "frameImages", "frameHashes"), targetVmUuids=[VM_UUID])

    class Capture:
        def __init__(self, count=2):
            self.binding = {"schema": "projectbuddy/active-capture-binding@1", "captureRunId": RUN_ID,
                            "grantRunId": grant["runId"], "vmUuid": VM_UUID, "actorSource": pinned["source"],
                            "contractSha256": module.sha256(pinned)}
            self.count = count
            self.polled = 0
            self.verified = 0
            self.incomplete = False

        @property
        def final(self):
            return self.polled == self.count and self.verified == self.count

        async def poll(self, limit):
            self.polled += 1
            image = png(3, 2, self.polled)
            return {"state": "stopped" if self.polled == self.count else "capturing", "incomplete": self.incomplete,
                    "frames": [{"sequence": self.polled, "sha256": hashlib.sha256(image).hexdigest()}], "samples": []}

        async def read_frame(self, sequence):
            self.verified += 1
            return png(3, 2, sequence)

    class Model(module.LocalOllama):
        def __init__(self):
            super().__init__("http://127.0.0.1:11435")
            self.payloads = []
            self.delay = 0
            self.partial = False
            self.started = asyncio.Event()
            self.closed = 0
            self.digest = MODEL["digest"]
            self.rebind_after = False
            self.eval_count = 40
            self.terminal = True
            self.observed_state = {"reading": 46, "limit": 64}

        async def tags_async(self):
            return {"models": [{"name": MODEL["tag"], "digest": self.digest}]}

        async def show_async(self, model):
            return {"model_info": {"general.architecture": "qwen2", "qwen2.context_length": 32768,
                                   "tokenizer.ggml.model": "gpt2"}, "template": "", "system": ""}

        async def stream_chat(self, payload):
            self.payloads.append(payload)
            selected = json.loads(payload["messages"][1]["content"])["captureMetadata"]
            choice = {"action": "approve" if len(self.payloads) == 1 else "hold", "captureRunId": RUN_ID,
                      "frameId": selected["frameId"], "observedState": self.observed_state}
            text = json.dumps(choice)
            self.started.set()
            try:
                yield {"content": text[:5], "done": False, "evalCount": None}
                await asyncio.sleep(self.delay)
                if self.partial:
                    raise OSError("private canary model failure")
                if self.rebind_after:
                    self.digest = "0" * 64
                yield {"content": text[5:], "done": self.terminal, "evalCount": self.eval_count}
            finally:
                self.closed += 1

    def create(mode="continuous", count=2, clock=None, **overrides):
        capture = Capture(count)
        model = Model()
        policy = {"schema": "projectbuddy/assisted-live-session@1", "mode": mode, "queueLimit": 3,
                  "maxPolls": 8, "pollSeconds": 0.001, "periodSeconds": 0.001 if mode == "periodic" else 0}
        policy.update(overrides)
        options = {"clock": clock} if clock is not None else {}
        return module.LiveCaptureSession(pinned, grant, capture, model, policy, **options), capture, model
    return module, pinned, grant, create


@pytest.mark.parametrize("mode", ["on-demand", "periodic", "continuous"])
def test_three_modes_keep_ordered_choices_and_never_grant_ui_authority(rig, mode):
    _, _, _, create = rig
    session, capture, model = create(mode)
    if mode == "on-demand":
        session.request_review()
        session.request_review()
    result = asyncio.run(session.run())
    assert result["status"] == "completed" and result["final"]
    assert not result["authorizesAction"] and capture.final
    assert [choice["action"] for choice in result["choices"]] == ["approve", "hold"]
    assert all(choice["frameId"].endswith(f":{index}") for index, choice in enumerate(result["choices"], 1))
    assert model.closed == 2 and result["requestCount"] == 8
    assert all(payload["options"]["num_gpu"] == 0 and payload["stream"] for payload in model.payloads)
    times = [event["monotonicSeconds"] for event in result["events"]]
    assert times == sorted(times)
    assert all(event["captureRunId"] == RUN_ID and event["vmUuid"] == VM_UUID for event in result["events"])
    assert {"request", "first-token", "final-token", "selected-action", "result"}.issubset(event["kind"] for event in result["events"])


@pytest.mark.parametrize("mode", ["on-demand", "periodic", "continuous"])
def test_partial_stream_failure_suppresses_choices_without_retry(rig, mode):
    _, _, _, create = rig
    session, _, model = create(mode)
    model.partial = True
    if mode == "on-demand":
        session.request_review()
    result = asyncio.run(session.run())
    assert result["status"] == "incomplete" and result["choices"] == [] and not result["final"]
    assert len(model.payloads) == model.closed == 1
    assert "canary" not in json.dumps(result)


@pytest.mark.parametrize("mode", ["on-demand", "periodic", "continuous"])
def test_queue_saturation_is_explicit_incomplete(rig, mode):
    _, _, _, create = rig
    session, _, model = create(mode, count=3, queueLimit=1)
    model.delay = 0.05
    if mode == "on-demand":
        session.request_review()
    result = asyncio.run(session.run())
    assert result["status"] == "incomplete" and not result["choices"]
    assert len(model.payloads) <= 1


def test_request_budget_exhaustion_never_retries(rig):
    _, _, grant, create = rig
    grant["reviewLifecycle"]["maxRequests"] = 2
    session, _, model = create()
    result = asyncio.run(session.run())
    assert result["requestCount"] == 2 and result["status"] == "incomplete" and not model.payloads


def test_model_digest_drift_prevents_inference(rig):
    _, _, _, create = rig
    session, _, model = create()
    model.digest = "0" * 64
    result = asyncio.run(session.run())
    assert result["status"] == "incomplete" and not model.payloads


@pytest.mark.parametrize("mode", ["on-demand", "periodic", "continuous"])
def test_cancellation_awaits_stream_cleanup(rig, mode):
    _, _, _, create = rig
    session, _, model = create(mode)
    if mode == "on-demand":
        session.request_review()
    model.delay = 10
    cancel = asyncio.Event()

    async def run():
        operation = asyncio.create_task(session.run(cancel))
        await model.started.wait()
        cancel.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert model.closed == 1 and session.result["status"] == "incomplete"
        assert not session.result["choices"]
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["digest", "tokens", "terminal", "capture"])
@pytest.mark.parametrize("mode", ["on-demand", "periodic", "continuous"])
def test_stream_or_capture_drift_never_returns_choices(rig, mode, failure):
    _, _, _, create = rig
    session, capture, model = create(mode)
    if mode == "on-demand":
        session.request_review()
    if failure == "digest":
        model.rebind_after = True
    elif failure == "tokens":
        model.eval_count = 99999
    elif failure == "terminal":
        model.terminal = False
    else:
        capture.incomplete = True
    result = asyncio.run(session.run())
    assert result["status"] == "incomplete" and not result["choices"] and not result["authorizesAction"]


def test_demand_overflow_poisoning_cannot_start_inference(rig):
    module, _, _, create = rig
    session, _, model = create("on-demand", queueLimit=1)
    session.request_review()
    with pytest.raises(module.LiveSessionError):
        session.request_review()
    result = asyncio.run(session.run())
    assert result["status"] == "incomplete" and not model.payloads


def test_pre_cancelled_session_does_not_contact_model(rig):
    _, _, _, create = rig
    session, _, model = create()
    cancel = asyncio.Event()
    cancel.set()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(session.run(cancel))
    assert session.result["status"] == "incomplete" and session.result["requestCount"] == 0
    assert not model.payloads


@pytest.mark.parametrize("field,value", [("maxRetries", 1), ("backoffSeconds", 1)])
def test_retry_policy_cannot_be_enabled(rig, field, value):
    module, _, grant, create = rig
    grant["reviewLifecycle"][field] = value
    with pytest.raises(module.LiveSessionError):
        create()


def test_on_demand_does_not_infer_without_an_explicit_trigger(rig):
    _, _, _, create = rig
    session, _, model = create("on-demand")
    result = asyncio.run(session.run())
    assert result["status"] == "incomplete" and not model.payloads


def test_live_session_is_one_shot(rig):
    module, _, _, create = rig
    session, _, _ = create()

    async def run():
        await session.run()
        with pytest.raises(module.LiveSessionError):
            await session.run()
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["on-demand", "periodic", "continuous"])
@pytest.mark.parametrize("failure", [None, "wrong-state", "partial", "ack"])
def test_scheduler_routes_one_live_action_through_real_sealed_broker(rig, live_choice_dispatcher, mode, failure):
    module, _, _, create = rig
    dispatcher, broker, workload, calls, fail_ack, _, _ = live_choice_dispatcher
    _, source, model = create(count=1)
    model.partial = failure == "partial"
    if failure == "wrong-state":
        model.observed_state = {"reading": 0, "limit": 0}
    fail_ack[0] = failure == "ack"

    class Capture:
        binding = dispatcher.binding
        incomplete = False
        state = "capturing"

        @property
        def final(self):
            return self.state == "stopped" and source.final

        async def poll(self, limit):
            if source.polled == 0:
                batch = await source.poll(limit)
                batch["state"] = "capturing"
                return batch
            self.state = "stopped" if dispatcher.applied else "capturing"
            return {"state": self.state, "incomplete": False, "frames": [], "samples": []}

        async def read_frame(self, sequence):
            return await source.read_frame(sequence)
    capture = Capture()
    policy = {"schema": "projectbuddy/assisted-live-session@1", "mode": mode, "queueLimit": 1,
              "maxPolls": 128, "pollSeconds": 0.001, "periodSeconds": 0.001 if mode == "periodic" else 0}
    session = module.LiveCaptureSession(broker.contract, broker.authorization, capture, model, policy,
                                       actor_server_id="lba", action_dispatcher=dispatcher)
    if mode == "on-demand":
        session.request_review()
    result = asyncio.run(session.run())
    if failure in {None, "wrong-state"}:
        assert result["status"] == "completed" and result["actionApplied"] and not result["actionOutcomeUnknown"]
        applied = [event for event in result["events"] if event["kind"] == "action-applied"]
        assert len(applied) == 1 and applied[0]["captureState"] == "capturing"
        assert workload.state == "decision_recorded" and len(calls) == 2
        from assisted_benchmark_synthetic_oracle import SyntheticUiOracle
        scores = SyntheticUiOracle().evaluate(workload, result["choices"][0]["observedState"])
        assert scores.state_grounded == (failure is None)
        assert scores.action_correct and scores.task_success
    else:
        assert result["status"] == "incomplete" and result["choices"] == [] and not result["actionApplied"]
        assert len(calls) == (2 if failure == "ack" else 1)
        assert result["actionOutcomeUnknown"] == (failure == "ack")
    assert result["authorizesAction"] is False


@pytest.mark.parametrize("observed", [None, {"reading": True, "limit": 64}, {"reading": 46.0, "limit": 64},
                                     {"reading": -1, "limit": 64}, {"reading": 46, "limit": 64, "command": "unapproved"}])
def test_malformed_state_claim_is_not_admitted(rig, observed):
    _, _, _, create = rig
    session, _, model = create()
    model.observed_state = observed
    result = asyncio.run(session.run())
    assert result["status"] == "incomplete" and not result["choices"] and not result["actionApplied"]


@pytest.mark.skipif(not os.environ.get("PROJECTBUDDY_ACTOR_FIXTURE_SERVER"), reason="requires a compiled Actor candidate for temporary capture fixtures only")
@pytest.mark.parametrize("mode", ["on-demand", "periodic", "continuous"])
def test_sdk_capture_to_sealed_action_while_recording_is_active(rig, live_choice_dispatcher, tmp_path, mode):
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    module, _, _, create = rig
    capture_module = importlib.import_module("assisted_benchmark_active_capture")
    dispatcher, broker, workload, calls, _, _, _ = live_choice_dispatcher
    _, _, model = create(count=1)
    binding = dispatcher.binding
    directory = tmp_path / f"run-{binding['captureRunId']}"
    directory.mkdir()
    started = "2026-11-11T12:00:00Z"
    stopped = "2026-11-11T12:00:01Z"
    beacon = {"schema": capture_module.SESSION_SCHEMA, "runId": binding["captureRunId"], "state": "capturing",
              "workload": "labview-launch", "plane": "WIN", "source": "ffmpeg-gdigrab", "startedAt": started,
              "stoppedAt": None, "updatedAt": started, "availableEvidence": ["frames", "resource-samples"]}
    session_file = directory / "active-capture-session.json"
    session_file.write_text(json.dumps(beacon), encoding="utf-8")
    frame_file = directory / "frame-00001.png"
    frame_file.write_bytes(png(3, 2, 48))
    os.utime(frame_file, (1794398400.1, 1794398400.1))
    (directory / "resources.jsonl").write_text(json.dumps({"ms": 1794398400000, "cpuPct": 1}) + "\n", encoding="utf-8")
    original_dispatch = dispatcher.dispatch_selection

    async def dispatch_and_finalize_fixture(choice, context):
        assert context["captureState"] == "capturing"
        assert json.loads(session_file.read_text(encoding="utf-8"))["state"] == "capturing"
        applied = await original_dispatch(choice, context)
        assert applied and workload.state == "decision_recorded"
        beacon.update(state="stopped", stoppedAt=stopped, updatedAt=stopped)
        pending = directory / "pending-beacon.json"
        pending.write_text(json.dumps(beacon), encoding="utf-8")
        pending.replace(session_file)
        return applied
    dispatcher.dispatch_selection = dispatch_and_finalize_fixture

    async def run():
        node = shutil.which("node")
        assert node is not None
        server = Path(os.environ["PROJECTBUDDY_ACTOR_FIXTURE_SERVER"]).resolve(strict=True)
        parameters = StdioServerParameters(command=node, args=[str(server)], env={"LBA_CAPTURE_ROOT": str(tmp_path)})
        with open(os.devnull, "w") as errors:
            async with stdio_client(parameters, errlog=errors) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as client:
                    initialized = (await client.initialize()).model_dump(mode="json", by_alias=True, exclude_none=True)
                    tools = await client.list_tools()
                    snapshot = {"protocolVersion": initialized["protocolVersion"], "serverInfo": initialized["serverInfo"],
                                "tools": [tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in tools.tools]}
                    capture = capture_module.ActiveCaptureConsumer(broker.contract, snapshot, broker.authorization,
                                                                   binding, capture_module.McpResourceClient(client), actor_server_id="lba")
                    policy = {"schema": "projectbuddy/assisted-live-session@1", "mode": mode, "queueLimit": 1,
                              "maxPolls": 128, "pollSeconds": 0.001, "periodSeconds": 0.001 if mode == "periodic" else 0}
                    session = module.LiveCaptureSession(broker.contract, broker.authorization, capture, model, policy,
                                                       actor_server_id="lba", action_dispatcher=dispatcher)
                    if mode == "on-demand":
                        session.request_review()
                    result = await session.run()
                    assert result["status"] == "completed" and result["actionApplied"] and not result["actionOutcomeUnknown"]
                    assert len([event for event in result["events"] if event["kind"] == "action-applied"]) == 1
                    assert len(calls) == 2 and result["authorizesAction"] is False
    asyncio.run(run())


def test_overall_deadline_stops_before_inference(rig):
    _, _, _, create = rig
    now = [0.0]
    session, _, model = create(clock=lambda: now[0])
    original = model.tags_async

    async def late_inventory():
        now[0] += 301
        return await original()
    model.tags_async = late_inventory
    result = asyncio.run(session.run())
    assert result["status"] == "incomplete" and result["requestCount"] == 1 and not model.payloads


@pytest.mark.skipif(not os.environ.get("PROJECTBUDDY_ACTOR_FIXTURE_SERVER"), reason="requires an explicit local compiled Actor candidate; no real capture or inference")
@pytest.mark.parametrize("mode", ["on-demand", "periodic", "continuous"])
def test_sdk_actor_http_stream_and_scheduler_composition(rig, tmp_path, mode):
    import httpx
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    module, pinned, grant, create = rig
    capture_module = importlib.import_module("assisted_benchmark_active_capture")
    _, fake, _ = create(count=1)
    directory = tmp_path / f"run-{RUN_ID}"
    directory.mkdir()
    stamp = "2026-11-11T12:00:00Z"
    (directory / "active-capture-session.json").write_text(json.dumps({
        "schema": capture_module.SESSION_SCHEMA, "runId": RUN_ID, "state": "stopped", "workload": "labview-launch",
        "plane": "WIN", "source": "ffmpeg-gdigrab", "startedAt": stamp, "stoppedAt": stamp,
        "updatedAt": stamp, "availableEvidence": ["frames", "resource-samples"],
    }), encoding="utf-8")
    (directory / "frame-00001.png").write_bytes(png(3, 2, 48))
    (directory / "resources.jsonl").write_text(json.dumps({"ms": 1794398400000, "cpuPct": 1}) + "\n", encoding="utf-8")
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": MODEL["tag"], "digest": MODEL["digest"]}]})
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"model_info": {"general.architecture": "qwen2", "qwen2.context_length": 32768,
                                                           "tokenizer.ggml.model": "gpt2"}, "template": "", "system": ""})
        assert request.url.path == "/api/chat"
        payload = json.loads(request.content)
        metadata = json.loads(payload["messages"][1]["content"])["captureMetadata"]
        choice = json.dumps({"action": "hold", "captureRunId": RUN_ID, "frameId": metadata["frameId"],
                     "observedState": {"reading": 72, "limit": 64}})
        chunks = [{"model": MODEL["tag"], "done": False, "message": {"role": "assistant", "content": choice[:5]}},
                  {"model": MODEL["tag"], "done": True, "eval_count": 40, "message": {"role": "assistant", "content": choice[5:]}}]
        return httpx.Response(200, content=b"\n".join(json.dumps(chunk).encode() for chunk in chunks))

    async def run():
        node = shutil.which("node")
        assert node is not None
        server = Path(os.environ["PROJECTBUDDY_ACTOR_FIXTURE_SERVER"]).resolve(strict=True)
        parameters = StdioServerParameters(command=node, args=[str(server)], env={"LBA_CAPTURE_ROOT": str(tmp_path)})
        with open(os.devnull, "w") as errors:
            async with stdio_client(parameters, errlog=errors) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as client:
                    initialized = (await client.initialize()).model_dump(mode="json", by_alias=True, exclude_none=True)
                    tools = await client.list_tools()
                    snapshot = {"protocolVersion": initialized["protocolVersion"], "serverInfo": initialized["serverInfo"],
                                "tools": [tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in tools.tools]}
                    capture = capture_module.ActiveCaptureConsumer(pinned, snapshot, grant, fake.binding, capture_module.McpResourceClient(client))
                    transport = module.LocalOllama("http://127.0.0.1:11435", http_transport=httpx.MockTransport(handler))
                    policy = {"schema": "projectbuddy/assisted-live-session@1", "mode": mode, "queueLimit": 1,
                              "maxPolls": 4, "pollSeconds": 0.001, "periodSeconds": 0.001 if mode == "periodic" else 0}
                    session = module.LiveCaptureSession(pinned, grant, capture, transport, policy)
                    if mode == "on-demand":
                        session.request_review()
                    result = await session.run()
                    assert result["status"] == "completed" and result["choices"][0]["action"] == "hold"
                    assert result["authorizesAction"] is False and result["requestCount"] == 5
                    assert str(tmp_path) not in json.dumps(result)
    asyncio.run(run())
    assert calls == ["/api/tags", "/api/show", "/api/tags", "/api/chat", "/api/tags"]