from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY / "scripts"))

import assisted_benchmark_synthetic_ui as synthetic_ui
from assisted_benchmark_synthetic_oracle import SyntheticUiOracle
from assisted_benchmark_synthetic_ui_server import create_server
from assisted_benchmark_synthetic_ui import ScenarioFacts, SyntheticUiError, SyntheticUiWorkload, scenario_facts
from assisted_benchmark_calibration import CalibrationError, summarize_calibration


def test_seeded_ui_states_and_event_ids_are_repeatable():
    first = SyntheticUiWorkload(101, clock=lambda: 10)
    second = SyntheticUiWorkload(101, clock=lambda: 20)

    assert first.visible_view() == second.visible_view()
    first_receipt = first.submit_action("approve")
    second_receipt = second.submit_action("approve")
    assert first_receipt == second_receipt


@pytest.mark.parametrize("seed,expected_target", [(99, "approve"), (100, "hold"), (101, "approve")])
def test_independent_oracle_scores_seeded_task(seed, expected_target):
    workload = SyntheticUiWorkload(seed, clock=lambda: 1234)
    oracle = SyntheticUiOracle()
    facts = scenario_facts(seed)
    state_claim = {"reading": facts.reading, "limit": facts.limit}

    with pytest.raises(ValueError, match="no submitted action"):
        oracle.evaluate(workload, state_claim)

    acknowledgement = workload.submit_action(expected_target)
    result = oracle.evaluate(workload, state_claim)

    assert acknowledgement == {
        "accepted": True,
        "taskId": workload.task_id,
        "state": "decision_recorded",
        "eventId": f"{workload.task_id}-event-0001",
    }
    assert result.state_grounded is True
    assert result.action_correct is True
    assert result.task_success is True
    assert "oracle" not in repr(acknowledgement).lower()
    assert "expected" not in repr(acknowledgement).lower()


def test_wrong_action_is_recorded_without_leaking_success_or_expected_target():
    workload = SyntheticUiWorkload(100)
    acknowledgement = workload.submit_action("approve")
    facts = scenario_facts(100)
    result = SyntheticUiOracle().evaluate(workload, {"reading": facts.reading, "limit": facts.limit})

    assert acknowledgement["accepted"] is True
    assert acknowledgement["state"] == "decision_recorded"
    assert result.action_correct is False
    assert result.task_success is False
    assert "correct" not in repr(acknowledgement).lower()
    assert "success" not in repr(acknowledgement).lower()


def test_oracle_detects_a_faulty_workload_scenario_generator(monkeypatch):
    monkeypatch.setattr(
        synthetic_ui,
        "scenario_facts",
        lambda seed: ScenarioFacts(seed, 1, 0, "fault-injected scenario"),
    )
    workload = SyntheticUiWorkload(99)
    workload.submit_action("approve")

    result = SyntheticUiOracle().evaluate(workload, {"reading": 1, "limit": 0})

    assert result.state_grounded is False
    assert result.action_correct is True
    assert result.task_success is True


def test_oracle_scores_model_state_claim_separately_from_task_success():
    workload = SyntheticUiWorkload(99)
    workload.submit_action("approve")

    result = SyntheticUiOracle().evaluate(workload, {"reading": 0, "limit": 0})

    assert result.state_grounded is False
    assert result.action_correct is True
    assert result.task_success is True


def test_invalid_or_duplicate_action_does_not_allow_a_retry():
    workload = SyntheticUiWorkload(101)
    before = workload.visible_view()

    with pytest.raises(SyntheticUiError, match="target is not available"):
        workload.submit_action("oracle_result")
    assert workload.visible_view() == before

    workload.submit_action("approve")
    with pytest.raises(SyntheticUiError, match="exactly one action"):
        workload.submit_action("hold")


@pytest.mark.parametrize("seed", [-1, 2**32, True, 1.0])
def test_scenario_seed_is_strictly_bounded(seed):
    with pytest.raises(SyntheticUiError, match="seed is outside"):
        scenario_facts(seed)


def test_http_ui_exposes_public_state_and_has_no_action_write_route():
    workload = SyntheticUiWorkload(99)
    server = create_server(workload)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(f"{base_url}/api/view") as response:
            view = response.read().decode("utf-8")
        with urlopen(f"{base_url}/") as response:
            page = response.read().decode("utf-8")
        request = Request(f"{base_url}/api/action", data=b"{}", method="POST")
        with pytest.raises(HTTPError) as error:
            urlopen(request)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert '"reading":' in view and '"limit":' in view
    assert "expectedTarget" not in view and "oracle" not in view.lower()
    assert "decision_recorded" in page and "dataset.targetId" in page
    assert error.value.code == 405


def calibration_records():
    records = []
    for mode_index, mode in enumerate(("on-demand", "periodic", "continuous")):
        for seed_index, seed in enumerate((99, 100)):
            evidence = 1_000_000 + seed_index * 100_000
            first = evidence + (10_000 + mode_index * 1_000)
            final = first + 20_000
            action = evidence + 45_000 + mode_index * 2_000
            records.append({
                "trialId": f"trial-{seed}",
                "mode": mode,
                "workloadVersion": "projectbuddy/synthetic-ui-task@1",
                "scenarioSeed": seed,
                "modelDigest": "a" * 64,
                "modelSettingsSha256": "b" * 64,
                "hostProfile": "operator-host-profile-v1",
                "hostResourceReserve": "reserved-memory-4GiB",
                "guestImageSha256": "c" * 64,
                "guestResourceProfile": "2-vcpu-4096MiB",
                "captureConfigurationSha256": "d" * 64,
                "evidenceAvailableNs": evidence,
                "firstTokenNs": first,
                "finalTokenNs": final,
                "actionAppliedNs": action,
                "stateGrounded": seed_index == 0 or mode_index != 1,
                "actionCorrect": seed_index == 0,
                "taskSuccess": seed_index == 0,
            })
    return records


def test_calibration_summarizes_each_mode_separately_and_never_qualifies():
    summary = summarize_calibration(calibration_records())

    assert summary["qualification"] == "not-qualified"
    assert set(summary["modes"]) == {"on-demand", "periodic", "continuous"}
    assert summary["modes"]["on-demand"]["sampleCount"] == 2
    assert summary["modes"]["on-demand"]["firstTokenLatency"]["p50Ns"] == 10_000
    assert summary["modes"]["continuous"]["actionLatency"]["p50Ns"] == 49_000
    assert summary["modes"]["periodic"]["stateGroundingRate"] == 0.5
    assert "threshold" not in repr(summary).lower()
    assert summary["modes"]["on-demand"]["finalTokenLatency"]["p95Bootstrap95CiNs"] is not None


def test_calibration_rejects_mixed_seeds_and_inconsistent_configuration():
    records = calibration_records()
    records[-1]["scenarioSeed"] = 101
    with pytest.raises(CalibrationError, match="same scenario seed set"):
        summarize_calibration(records)

    records = calibration_records()
    records[-1]["modelDigest"] = "e" * 64
    with pytest.raises(CalibrationError, match="fixed environment and model"):
        summarize_calibration(records)


@pytest.mark.parametrize("problem", ["missing-field", "mode", "digest", "timestamp", "boolean"])
def test_calibration_rejects_malformed_records(problem):
    records = calibration_records()
    if problem == "missing-field":
        del records[0]["trialId"]
    elif problem == "mode":
        records[0]["mode"] = "unknown"
    elif problem == "digest":
        records[0]["modelDigest"] = "not-a-digest"
    elif problem == "timestamp":
        records[0]["firstTokenNs"] = records[0]["evidenceAvailableNs"] - 1
    else:
        records[0]["taskSuccess"] = 1
    with pytest.raises(CalibrationError):
        summarize_calibration(records)


def test_calibration_single_sample_reports_unestimated_uncertainty():
    records = [record for record in calibration_records() if record["scenarioSeed"] == 99]
    summary = summarize_calibration(records)

    assert all(mode["sampleCount"] == 1 for mode in summary["modes"].values())
    assert all(mode["finalTokenLatency"]["p95Bootstrap95CiNs"] is None for mode in summary["modes"].values())


def test_stdio_mcp_server_exposes_only_exact_ui_action_and_returns_no_oracle_data():
    import socket
    from urllib.request import urlopen

    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    async def exercise_server():
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            ui_port = listener.getsockname()[1]
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[str(REPOSITORY / "scripts" / "assisted_benchmark_synthetic_ui_mcp_server.py"),
                  "--seed", "100", "--ui-port", str(ui_port)],
            cwd=REPOSITORY,
        )
        async with stdio_client(parameters, errlog=subprocess.DEVNULL) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                initialization = await session.initialize()
                assert initialization.server_info.name == "projectbuddy-synthetic-ui"
                assert initialization.server_info.version == "1.0.0"
                tools = await session.list_tools()
                assert [tool.name for tool in tools.tools] == ["click_target"]
                schema = tools.tools[0].input_schema
                assert schema == {
                    "type": "object",
                    "properties": {
                        "taskId": {"title": "Taskid", "type": "string"},
                        "targetId": {"enum": ["approve", "hold"], "title": "Targetid", "type": "string"},
                    },
                    "required": ["taskId", "targetId"],
                    "title": "click_targetArguments",
                    "additionalProperties": False,
                }
                with urlopen(f"http://127.0.0.1:{ui_port}/api/view") as response:
                    before_action = json.loads(response.read())
                altered = await session.call_tool(
                    "click_target",
                    {"taskId": "synthetic-00000064", "targetId": "hold", "extra": True},
                )
                assert altered.is_error is True
                result = await session.call_tool(
                    "click_target",
                    {"taskId": "synthetic-00000064", "targetId": "hold"},
                )
                assert result.is_error is not True
                duplicate = await session.call_tool(
                    "click_target",
                    {"taskId": "synthetic-00000064", "targetId": "approve"},
                )
                assert duplicate.is_error is True
                with urlopen(f"http://127.0.0.1:{ui_port}/api/view") as response:
                    after_action = json.loads(response.read())
                return result.structured_content, before_action, after_action

    result, before_action, after_action = asyncio.run(exercise_server())

    assert result == {
        "accepted": True,
        "taskId": "synthetic-00000064",
        "state": "decision_recorded",
        "eventId": "synthetic-00000064-event-0001",
    }
    assert "oracle" not in repr(result).lower()
    assert "expected" not in repr(result).lower()
    assert before_action["state"] == "awaiting_decision"
    assert after_action["state"] == "decision_recorded"


def test_mcp_handlers_reject_wrong_task_malformed_action_and_duplicate_submission():
    from mcp.types import CallToolRequestParams

    from assisted_benchmark_synthetic_ui_mcp_server import UI_TOOL_INPUT_SCHEMA, create_mcp_server

    workload = SyntheticUiWorkload(99)
    server = create_mcp_server(workload)
    listed = asyncio.run(server.get_request_handler("tools/list").handler(None, None))
    assert listed.tools[0].input_schema == UI_TOOL_INPUT_SCHEMA
    handler = server.get_request_handler("tools/call").handler

    invalid_requests = (
        CallToolRequestParams(name="unknown_tool", arguments={"taskId": workload.task_id, "targetId": "approve"}),
        CallToolRequestParams(name="click_target", arguments={"taskId": workload.task_id, "targetId": ["approve"]}),
        CallToolRequestParams(name="click_target", arguments={"taskId": "other-task", "targetId": "approve"}),
        CallToolRequestParams(name="click_target", arguments={"taskId": workload.task_id, "targetId": "approve", "extra": True}),
    )
    for request in invalid_requests:
        result = asyncio.run(handler(None, request))
        assert result.is_error is True
        assert "oracle" not in repr(result).lower()
    assert workload.state == "awaiting_decision"

    valid_request = CallToolRequestParams(
        name="click_target", arguments={"taskId": workload.task_id, "targetId": "approve"}
    )
    assert asyncio.run(handler(None, valid_request)).is_error is False
    assert asyncio.run(handler(None, valid_request)).is_error is True