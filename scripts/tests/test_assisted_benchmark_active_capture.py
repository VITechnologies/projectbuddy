from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil

import pytest

from test_assisted_benchmark_capture import png
from test_assisted_benchmark_contract import REPOSITORY, VM_UUID, authorization, choice_authorization, load_contract, server_set_contract, snapshot

RUN_ID = "a0f4e148-02c2-46ac-a36d-dbdcb897187d"
URI = f"lba-capture://session/{RUN_ID}"
STAMP = "2026-11-11T12:00:00.000Z"


class Client:
    def __init__(self, session, batch, image):
        self.session = session
        self.batch = batch
        self.image = image
        self.calls = []

    async def read_resource(self, uri):
        self.calls.append(uri)
        if "/frame/" in uri:
            content = {"uri": uri, "mimeType": "image/png", "blob": base64.b64encode(self.image).decode()}
        else:
            content = {"uri": uri, "mimeType": "application/json", "text": json.dumps(self.batch if "/batch?" in uri else self.session)}
        return {"contents": [content]}


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.syspath_prepend(str(REPOSITORY / "scripts"))
    module = importlib.import_module("assisted_benchmark_active_capture")
    contract = load_contract()
    contract["captureResources"] = copy.deepcopy(module.RESOURCE_CONTRACT)
    grant = authorization(contract, evidenceFields=["captureMetadata", "frameHashes", "frameImages", "resourceSamples"])
    binding = {"schema": "projectbuddy/active-capture-binding@1", "grantRunId": grant["runId"],
               "captureRunId": RUN_ID, "vmUuid": VM_UUID, "actorSource": copy.deepcopy(contract["source"]),
               "contractSha256": module.sha256(contract)}
    image = png(3, 2, 48)
    digest = hashlib.sha256(image).hexdigest()
    session = {"schema": module.SESSION_SCHEMA, "runId": RUN_ID, "workload": "labview-launch", "state": "stopped",
               "plane": "WIN", "source": "ffmpeg-gdigrab", "startedAt": STAMP, "updatedAt": STAMP,
               "stoppedAt": STAMP, "availableEvidence": ["frames", "resource-samples"],
               "watermarks": {"frameSequence": 1, "sampleSequence": 1}, "incomplete": False}
    batch = {"schema": module.BATCH_SCHEMA, "runId": RUN_ID, "state": "stopped",
             "after": {"frame": 0, "sample": 0}, "next": {"frame": 1, "sample": 1}, "watermarks": {"frame": 1, "sample": 1},
             "frames": [{"sequence": 1, "capturedAt": STAMP, "sizeBytes": len(image), "sha256": digest,
                         "uri": f"{URI}/frame/1?sha256={digest}"}],
             "samples": [{"sequence": 1, "capturedAt": STAMP, "cpuPct": 1, "ramMb": 2, "diskPct": None, "disks": []}],
             "frameGaps": [], "incompleteFrames": [], "incompleteSamples": [], "sampleTailIncomplete": False,
             "sampleLogUnavailable": False, "incomplete": False}
    client = Client(session, batch, image)

    def create(**options):
        return module.ActiveCaptureConsumer(contract, snapshot(contract), grant, binding, client, **options)

    return module, contract, grant, binding, client, create


def test_stopped_evidence_is_final_only_after_explicit_frame_verification(setup):
    module, _, _, _, client, create = setup
    consumer = create()

    async def run():
        result = await consumer.poll()
        assert result["state"] == "stopped" and not result["final"]
        assert not consumer.final
        assert await consumer.read_frame(1) == client.image
        assert consumer.final
    asyncio.run(run())
    assert len(client.calls) == 3


@pytest.mark.parametrize("field,value", [("vmUuid", "b" * 36), ("grantRunId", "other"),
                                           ("captureRunId", "../private"), ("contractSha256", "0" * 64),
                                           ("actorSource", {})])
def test_binding_drift_refused_before_any_read(setup, field, value):
    module, _, _, binding, client, create = setup
    binding[field] = value
    with pytest.raises(module.ActiveCaptureError):
        create()
    assert not client.calls


@pytest.mark.parametrize("mutation", [
    lambda batch: batch.update(runId="other"),
    lambda batch: batch.update(schema="other"),
    lambda batch: batch["after"].update(frame=1),
    lambda batch: batch["after"].update(frame=False),
    lambda batch: batch["next"].update(frame=2),
    lambda batch: batch["frames"].append(copy.deepcopy(batch["frames"][0])),
    lambda batch: batch["frames"][0].update(sequence=2),
    lambda batch: batch["frames"][0].update(uri="file:///private/canary"),
    lambda batch: batch["samples"][0].update(cpuPct=float("nan")),
    lambda batch: batch["samples"][0].update(capturedAt="not-a-date"),
    lambda batch: batch["samples"][0].update(capturedAt="2020-01-01T00:00:00Z"),
    lambda batch: batch["samples"][0].update(disks=[{"name": "/private/canary", "writeMBs": 0, "readMBs": 0}]),
])
def test_malformed_batches_fail_sticky_without_retries_or_path_leaks(setup, mutation):
    module, _, _, _, client, create = setup
    mutation(client.batch)
    consumer = create()

    async def run():
        with pytest.raises(module.ActiveCaptureError) as error:
            await consumer.poll()
        assert "canary" not in str(error.value)
        assert consumer.incomplete and not consumer.final
        calls = len(client.calls)
        with pytest.raises(module.ActiveCaptureError):
            await consumer.poll()
        assert len(client.calls) == calls
    asyncio.run(run())


@pytest.mark.parametrize("state", ["capturing", "finalizing", "failed"])
def test_nonstopped_states_never_become_final(setup, state):
    _, _, _, _, client, create = setup
    client.session["state"] = client.batch["state"] = state
    if state != "failed":
        client.session["stoppedAt"] = None
    consumer = create()
    asyncio.run(consumer.poll())
    assert not consumer.final


@pytest.mark.parametrize("marker,value", [("frameGaps", [{"from": 2, "to": 3}]),
                                          ("incompleteFrames", [2]), ("incompleteSamples", [2]),
                                          ("sampleTailIncomplete", True), ("sampleLogUnavailable", True)])
def test_explicit_partial_evidence_is_incomplete_not_final(setup, marker, value):
    _, _, _, _, client, create = setup
    client.batch[marker] = value
    client.batch["incomplete"] = True
    consumer = create()
    result = asyncio.run(consumer.poll())
    assert result["incomplete"] and result["frames"] == [] and not consumer.final


def test_tampered_frame_refused_without_retry(setup):
    module, _, _, _, client, create = setup
    consumer = create()

    async def run():
        await consumer.poll()
        client.image = b"not-the-advertised-image"
        with pytest.raises(module.ActiveCaptureError):
            await consumer.read_frame(1)
        assert consumer.incomplete and not consumer.final
    asyncio.run(run())


def test_read_budget_is_sticky_and_never_implicitly_retries(setup):
    module, _, _, _, client, create = setup
    consumer = create(max_reads=1)
    with pytest.raises(module.ActiveCaptureError):
        asyncio.run(consumer.poll())
    assert len(client.calls) == 1 and consumer.incomplete


def test_current_production_contract_cannot_enable_active_reads(setup):
    module, contract, grant, binding, client, _ = setup
    del contract["captureResources"]
    binding["contractSha256"] = module.sha256(contract)
    with pytest.raises(module.ActiveCaptureError):
        module.ActiveCaptureConsumer(contract, snapshot(contract), grant, binding, client)
    assert not client.calls


def test_local_capture_pin_is_immutable_and_does_not_enable_default_contract(setup):
    module, _, _, _, _, _ = setup
    candidate = module.load_active_capture_contract()
    assert candidate["source"]["revision"] == "06630fa47ad53d5a21ce490532811e1894b005fa"
    assert candidate["captureResources"] == module.RESOURCE_CONTRACT
    assert "captureResources" not in load_contract()


@pytest.mark.parametrize("field,value", [("activation", "production"), ("baseContractSha256", "0" * 64),
                                       ("captureResources", {}), ("source", {})])
def test_local_capture_pin_drift_is_refused(setup, tmp_path, field, value):
    module, _, _, _, _, _ = setup
    pin = json.loads(module.DEFAULT_ACTIVE_PIN.read_text(encoding="utf-8"))
    pin[field] = value
    path = tmp_path / "pin.json"
    path.write_text(json.dumps(pin), encoding="utf-8")
    with pytest.raises(module.ActiveCaptureError):
        module.load_active_capture_contract(path)


def test_session_batch_state_race_cannot_finalize_provisional_session(setup):
    _, _, _, _, client, create = setup
    client.session.update(state="capturing", stoppedAt=None)
    consumer = create()

    async def run():
        await consumer.poll()
        await consumer.read_frame(1)
        assert not consumer.final
        client.session.update(state="stopped", stoppedAt=STAMP)
        client.batch.update(after={"frame": 1, "sample": 1}, frames=[], samples=[])
        await consumer.poll()
        assert consumer.final
    asyncio.run(run())


def test_capture_lifecycle_regression_is_rejected(setup):
    module, _, _, _, client, create = setup
    consumer = create()

    async def run():
        await consumer.poll()
        client.session.update(state="capturing", stoppedAt=None)
        with pytest.raises(module.ActiveCaptureError):
            await consumer.poll()
    asyncio.run(run())


def test_cancellation_settles_pending_read_and_disables_retries(setup):
    _, _, _, _, client, create = setup
    started = asyncio.Event()
    settled = asyncio.Event()

    async def pending(uri):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            settled.set()
    client.read_resource = pending
    consumer = create()

    async def run():
        task = asyncio.create_task(consumer.poll())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert settled.is_set() and consumer.incomplete and not consumer.final
    asyncio.run(run())


def test_deadline_exhaustion_refuses_without_reading(setup):
    module, _, _, _, client, create = setup
    now = [0.0]
    consumer = create(clock=lambda: now[0], total_seconds=10, request_seconds=1)
    now[0] = 10.0
    with pytest.raises(module.ActiveCaptureError):
        asyncio.run(consumer.poll())
    assert consumer.incomplete and not client.calls


def test_late_transport_response_is_not_admitted(setup):
    module, _, _, _, client, create = setup
    now = [0.0]
    original = client.read_resource

    async def delayed(uri):
        result = await original(uri)
        now[0] += 2
        return result
    client.read_resource = delayed
    consumer = create(clock=lambda: now[0], total_seconds=10, request_seconds=1)
    with pytest.raises(module.ActiveCaptureError):
        asyncio.run(consumer.poll())
    assert consumer.incomplete and len(client.calls) == 1


def test_timeout_settles_transport_without_retry(setup):
    module, _, _, _, client, create = setup
    settled = []

    async def pending(uri):
        client.calls.append(uri)
        try:
            await asyncio.Event().wait()
        finally:
            settled.append(True)
    client.read_resource = pending
    consumer = create(request_seconds=0.01)
    with pytest.raises(module.ActiveCaptureError):
        asyncio.run(consumer.poll())
    assert settled == [True] and len(client.calls) == 1 and consumer.incomplete


@pytest.mark.parametrize("response", [
    {"contents": []},
    {"contents": [{"uri": "file:///private/canary", "mimeType": "application/json", "text": "{}"}]},
    {"contents": [{"uri": URI, "mimeType": "application/json", "text": '{"state":1,"state":2}'}]},
    {"contents": [{"uri": URI, "mimeType": "application/json", "text": "x" * (128 * 1024 + 1)}]},
])
def test_untrusted_resource_envelopes_fail_without_leaking_details(setup, response):
    module, _, _, _, client, create = setup

    async def read(uri):
        return response
    client.read_resource = read
    consumer = create()
    with pytest.raises(module.ActiveCaptureError) as error:
        asyncio.run(consumer.poll())
    assert "canary" not in str(error.value) and consumer.incomplete


def test_matching_hash_does_not_make_invalid_png_valid(setup):
    module, _, _, _, client, create = setup
    client.image = b"not-a-png"
    frame = client.batch["frames"][0]
    digest = hashlib.sha256(client.image).hexdigest()
    frame.update(sha256=digest, sizeBytes=len(client.image), uri=f"{URI}/frame/1?sha256={digest}")
    consumer = create()

    async def run():
        await consumer.poll()
        with pytest.raises(module.ActiveCaptureError):
            await consumer.read_frame(1)
        assert not consumer.final
    asyncio.run(run())


def test_server_snapshot_drift_refused_before_transport(setup):
    module, contract, grant, binding, client, _ = setup
    capabilities = snapshot(contract)
    capabilities["serverInfo"]["version"] = "0.0.0"
    with pytest.raises(ValueError):
        module.ActiveCaptureConsumer(contract, capabilities, grant, binding, client)
    assert not client.calls


def test_resource_consumer_accepts_explicit_actor_in_shared_server_set_grant(setup):
    module, actor, _, binding, client, _ = setup
    server_set = server_set_contract()
    server_set["servers"]["lba"] = actor
    server_set["sourceRevision"] = "b" * 40
    grant = choice_authorization(server_set, evidenceFields=["captureMetadata", "frameHashes", "frameImages", "resourceSamples"])
    consumer = module.ActiveCaptureConsumer(server_set, snapshot(actor), grant, binding, client, actor_server_id="lba")
    result = asyncio.run(consumer.poll())
    assert result["state"] == "stopped" and not result["final"]


def test_server_set_resource_reads_require_explicit_actor_identity(setup):
    module, actor, _, binding, client, _ = setup
    server_set = server_set_contract()
    server_set["servers"]["lba"] = actor
    grant = choice_authorization(server_set, evidenceFields=["captureMetadata", "frameHashes", "frameImages", "resourceSamples"])
    with pytest.raises(ValueError):
        module.ActiveCaptureConsumer(server_set, snapshot(actor), grant, binding, client)
    assert not client.calls


def test_observed_watermarks_cannot_shrink_to_hide_missing_evidence(setup):
    module, _, _, _, client, create = setup
    client.session.update(state="capturing", stoppedAt=None)
    client.session["watermarks"]["frameSequence"] = 2
    client.batch.update(state="capturing")
    client.batch["watermarks"]["frame"] = 2
    consumer = create()

    async def run():
        await consumer.poll(limit=1)
        client.session["watermarks"]["frameSequence"] = 1
        with pytest.raises(module.ActiveCaptureError):
            await consumer.poll()
        assert consumer.incomplete
    asyncio.run(run())


@pytest.mark.skipif(not os.environ.get("PROJECTBUDDY_ACTOR_FIXTURE_SERVER"), reason="requires an explicit compiled Actor candidate for local fixture-only interop")
@pytest.mark.parametrize("adapter", ["raw", "sdk"])
def test_compiled_actor_stdio_fixture_interoperability(setup, tmp_path, adapter):
    module, contract, grant, binding, fake, create = setup
    if adapter == "sdk":
        contract = module.load_active_capture_contract()
        grant["sourceRevision"] = contract["source"]["revision"]
        binding["actorSource"] = copy.deepcopy(contract["source"])
        binding["contractSha256"] = module.sha256(contract)
    server = Path(os.environ["PROJECTBUDDY_ACTOR_FIXTURE_SERVER"]).resolve(strict=True)
    node = shutil.which("node")
    assert node is not None
    run_directory = tmp_path / f"run-{RUN_ID}"
    run_directory.mkdir()
    session = copy.deepcopy(fake.session)
    del session["watermarks"]
    del session["incomplete"]
    (run_directory / "active-capture-session.json").write_text(json.dumps(session), encoding="utf-8")
    (run_directory / "frame-00001.png").write_bytes(fake.image)
    (run_directory / "resources.jsonl").write_text(json.dumps({"ms": 1794398400000, "cpuPct": 1, "ramMb": 2, "diskPct": None, "disks": []}) + "\n", encoding="utf-8")

    async def run():
        process = await asyncio.create_subprocess_exec(node, str(server), env={**os.environ, "LBA_CAPTURE_ROOT": str(tmp_path)},
                                                       stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                       stderr=asyncio.subprocess.DEVNULL)
        calls = []

        async def read(uri):
            request_id = len(calls) + 1
            calls.append(uri)
            process.stdin.write((json.dumps({"jsonrpc": "2.0", "id": request_id, "method": "resources/read", "params": {"uri": uri}}) + "\n").encode())
            await process.stdin.drain()
            response = json.loads(await process.stdout.readline())
            assert response["id"] == request_id and "error" not in response
            return response["result"]
        fake.read_resource = read
        consumer = create()
        try:
            result = await consumer.poll()
            assert result["state"] == "stopped" and not result["final"]
            assert await consumer.read_frame(1) == fake.image
            assert consumer.final and len(calls) == 3
            assert all(uri.startswith(URI) for uri in calls)
        finally:
            if process.returncode is None:
                process.terminate()
            await asyncio.wait_for(process.wait(), 5)

    async def run_sdk():
        import subprocess
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        parameters = StdioServerParameters(command=node, args=[str(server)], env={"LBA_CAPTURE_ROOT": str(tmp_path)})
        with open(os.devnull, "w") as errors:
            async with stdio_client(parameters, errlog=errors) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as client:
                    initialized = await client.initialize()
                    listed = await client.list_tools()
                    initialization = initialized.model_dump(mode="json", by_alias=True, exclude_none=True)
                    capabilities = {"protocolVersion": initialization["protocolVersion"],
                                    "serverInfo": initialization["serverInfo"],
                                    "tools": [tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in listed.tools]}
                    consumer = module.ActiveCaptureConsumer(contract, capabilities, grant, binding, module.McpResourceClient(client))
                    result = await consumer.poll()
                    assert not result["final"] and result["state"] == "stopped"
                    assert await consumer.read_frame(1) == fake.image
                    assert consumer.final
    asyncio.run(run_sdk() if adapter == "sdk" else run())