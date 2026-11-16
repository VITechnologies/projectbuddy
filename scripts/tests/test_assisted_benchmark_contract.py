from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
CONTRACT_PATH = REPOSITORY / "tools" / "assisted-benchmark" / "lba-mcp-contract.json"
MODULE_PATH = REPOSITORY / "scripts" / "assisted_benchmark_contract.py"
VM_UUID = "afc966a5-f120-4d3e-9760-69fd6344a03f"


def load_module():
    spec = importlib.util.spec_from_file_location("assisted_benchmark_contract_test", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load contract module from {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_contract():
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def snapshot(contract, groups=("core", "acg-grid")):
    tools = []
    for group_name in groups:
        for expected in contract["toolGroups"][group_name]["tools"]:
            tools.append({
                "name": expected["name"],
                "description": "test fixture tool",
                "inputSchema": copy.deepcopy(expected["inputSchema"]),
            })
    source = contract["source"]
    return {
        "protocolVersion": source["protocolVersion"],
        "serverInfo": {"name": source["serverName"], "version": source["serverVersion"]},
        "tools": tools,
    }


def authorization(contract, **overrides):
    data = {
        "schema": contract["authorizationSchema"],
        "runId": "run-0001",
        "workload": "labview-launch",
        "sourceRevision": "7f65a2215b952e1f31bc3677087f14c8b528d789",
        "approvedBy": "operator",
        "approvedAt": "2026-11-10T12:00:00Z",
        "approvedCalls": [
            {"tool": "get_benchmark_series", "arguments": {}},
            {"tool": "teardown", "arguments": {"plane": "VBOX", "id": "witness-1"}},
        ],
        "targetVmUuids": [VM_UUID],
        "ollamaModel": {
            "tag": "projectbuddy:0.1-qwen2.5-7b",
            "digest": "845dbda0ea48ed749caafd9e6037047aa19acfcfd82e704d7ca97d631a0b697e",
        },
        "evidenceFields": ["captureMetadata", "frameHashes"],
        "allowVmLifecycle": True,
        "teardownOnNativePplFailure": True,
    }
    data.update(overrides)
    return data


def server_set_contract():
    lba = load_contract()
    ui = copy.deepcopy(lba)
    ui["source"] = {
        "repository": "VITechnologies/projectbuddy",
        "revision": "a" * 40,
        "serverName": "projectbuddy-synthetic-ui",
        "serverVersion": "1.0.0",
        "protocolVersion": "2025-06-18",
    }
    ui["toolGroups"] = {
        "core": {
            "required": True,
            "tools": [{
                "name": "click_target",
                "effect": "coordination-write",
                "mutatesState": True,
                "requiresVmLifecycle": False,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "taskId": {"title": "Taskid", "type": "string"},
                        "targetId": {"enum": ["approve", "hold"], "title": "Targetid", "type": "string"},
                    },
                    "required": ["taskId", "targetId"],
                    "title": "click_targetArguments",
                    "additionalProperties": False,
                },
            }],
        },
        "acg-grid": {"required": False, "tools": []},
    }
    return {
        "schema": "projectbuddy/assisted-mcp-server-set@1",
        "sourceRevision": lba["source"]["revision"],
        "authorizationSchema": lba["authorizationSchema"],
        "allowedEvidenceFields": lba["allowedEvidenceFields"],
        "servers": {"lba": lba, "synthetic-ui": ui},
    }


def choice_authorization(server_set, **overrides):
    approved_calls = [{"serverId": "lba", "tool": "get_host_capabilities", "arguments": {}}]
    choices = [{
        "choiceId": "decision",
        "selection": "exactly-one",
        "candidates": [
            {"serverId": "synthetic-ui", "tool": "click_target",
             "arguments": {"taskId": "synthetic-00000063", "targetId": "approve"}},
            {"serverId": "synthetic-ui", "tool": "click_target",
             "arguments": {"taskId": "synthetic-00000063", "targetId": "hold"}},
        ],
    }]
    return authorization(server_set, sourceRevision=server_set["sourceRevision"],
                         approvedCalls=approved_calls, approvedCallChoices=choices, **overrides)


def test_checked_in_contract_matches_actor_core_and_optional_grid_surface():
    module = load_module()
    contract = load_contract()
    report = module.validate_snapshot(snapshot(contract), contract)

    assert report["serverName"] == "labview-benchmark-actor"
    assert report["serverVersion"] == "1.4.14"
    assert report["toolCount"] == 13
    assert report["availableGroups"] == ["core", "acg-grid"]
    assert report["missingOptionalGroups"] == []


def test_core_only_server_is_supported_but_reports_missing_optional_grid():
    module = load_module()
    contract = load_contract()
    report = module.validate_snapshot(snapshot(contract, groups=("core",)), contract)

    assert report["toolCount"] == 4
    assert report["availableGroups"] == ["core"]
    assert report["missingOptionalGroups"] == ["acg-grid"]


@pytest.mark.parametrize("change", [
    lambda value: value.update(protocolVersion="2025-03-26"),
    lambda value: value["serverInfo"].update(name="other-server"),
    lambda value: value["serverInfo"].update(version="1.4.15"),
])
def test_protocol_and_server_drift_fail_closed(change):
    module = load_module()
    contract = load_contract()
    value = snapshot(contract)
    change(value)

    with pytest.raises(module.ContractError):
        module.validate_snapshot(value, contract)


def test_missing_core_tool_fails_closed():
    module = load_module()
    contract = load_contract()
    value = snapshot(contract)
    value["tools"] = [tool for tool in value["tools"] if tool["name"] != "get_host_capabilities"]

    with pytest.raises(module.ContractError, match="core tool group is incomplete"):
        module.validate_snapshot(value, contract)


def test_unknown_tool_fails_closed_without_echoing_its_description():
    module = load_module()
    contract = load_contract()
    value = snapshot(contract, groups=("core",))
    value["tools"].append({
        "name": "unreviewed_tool",
        "description": "SECRET_CANARY",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    })

    with pytest.raises(module.ContractError, match="unknown tool") as error:
        module.validate_snapshot(value, contract)
    assert "SECRET_CANARY" not in str(error.value)


def test_schema_drift_and_partial_optional_group_fail_closed():
    module = load_module()
    contract = load_contract()
    changed = snapshot(contract)
    changed["tools"][0]["inputSchema"]["additionalProperties"] = True
    with pytest.raises(module.ContractError, match="input schema drifted"):
        module.validate_snapshot(changed, contract)

    partial = snapshot(contract, groups=("core", "acg-grid"))
    partial["tools"] = [tool for tool in partial["tools"] if tool["name"] != "run_quorum"]
    with pytest.raises(module.ContractError, match="ACG grid tool group is incomplete"):
        module.validate_snapshot(partial, contract)


def test_per_run_approval_allows_only_named_tools_and_never_executes_plan_text():
    module = load_module()
    contract = load_contract()
    auth = authorization(contract)
    allowed = module.authorize_tool_call("get_benchmark_series", {}, auth, contract)
    assert allowed["allowed"] is True
    assert allowed["effect"] == "read-only"
    assert allowed["ollamaModel"]["tag"] == "projectbuddy:0.1-qwen2.5-7b"
    assert allowed["executesMcpResultCommands"] is False

    plan = module.authorize_tool_call("teardown", {"plane": "VBOX", "id": "witness-1"}, auth, contract)
    assert plan["effect"] == "plan-only"
    assert plan["executesMcpResultCommands"] is False
    with pytest.raises(module.ContractError, match="outside the per-run approval"):
        module.authorize_tool_call("run_quorum", {"bundles": [{}]}, auth, contract)


def test_plan_only_grid_tool_does_not_require_vm_lifecycle_authority():
    module = load_module()
    contract_value = load_contract()
    auth = authorization(
        contract_value,
        allowVmLifecycle=False,
        teardownOnNativePplFailure=False,
        targetVmUuids=[],
    )
    result = module.authorize_tool_call("teardown", {"plane": "VBOX", "id": "witness-1"}, auth, contract_value)
    assert result["effect"] == "plan-only"
    assert result["mutatesState"] is False
    assert result["executesMcpResultCommands"] is False


def test_lifecycle_and_ppl_failure_cleanup_are_bound_to_the_approved_vm():
    module = load_module()
    contract = load_contract()
    auth = authorization(contract)
    allowed = module.authorize_ppl_failure_cleanup(auth, VM_UUID, contract)
    assert allowed == {"runId": "run-0001", "vmUuid": VM_UUID, "authorized": True}

    with pytest.raises(module.ContractError, match="does not match"):
        module.authorize_ppl_failure_cleanup(auth, "79549aeb-8d0c-4f89-af53-c70456a3710a", contract)
    with pytest.raises(module.ContractError, match="does not authorize"):
        module.authorize_ppl_failure_cleanup(
            authorization(contract, teardownOnNativePplFailure=False), VM_UUID, contract
        )


def test_invalid_authorization_fields_fail_closed():
    module = load_module()
    contract = load_contract()
    auth = authorization(contract, approvedCalls=[{"tool": "unknown_tool", "arguments": {}}])
    with pytest.raises(module.ContractError, match="unknown MCP tool"):
        module.validate_authorization(auth, contract)


def test_exactly_one_call_choice_authorizes_only_two_pinned_candidates():
    module = load_module()
    server_set = server_set_contract()
    auth = choice_authorization(server_set)

    validated = module.validate_authorization(auth, server_set)

    assert validated["approvedCallChoices"][0]["choiceId"] == "decision"
    assert len(validated["approvedCallChoices"][0]["candidates"]) == 2
    for candidate in validated["approvedCallChoices"][0]["candidates"]:
        assert module.authorize_tool_call(candidate["tool"], candidate["arguments"],
                                          validated, server_set, candidate["serverId"])["allowed"] is True
    with pytest.raises(module.ContractError, match="outside the pinned enum"):
        module.authorize_tool_call("click_target", {"taskId": "synthetic-00000063", "targetId": "delete"},
                                   validated, server_set, "synthetic-ui")


@pytest.mark.parametrize("problem, message", [
    ("candidate-count", "exactly two candidates"),
    ("duplicate", "duplicate or already approved candidate"),
    ("server", "MCP server ID is not in the pinned server set"),
    ("tool", "unknown MCP tool"),
])
def test_invalid_choice_candidate_is_rejected(problem, message):
    module = load_module()
    server_set = server_set_contract()
    auth = choice_authorization(server_set)
    candidates = auth["approvedCallChoices"][0]["candidates"]
    if problem == "candidate-count":
        candidates.pop()
    elif problem == "duplicate":
        candidates[1] = copy.deepcopy(candidates[0])
    elif problem == "server":
        candidates[1]["serverId"] = "other-server"
    else:
        candidates[1]["tool"] = "unknown-tool"

    with pytest.raises(module.ContractError, match=message):
        module.validate_authorization(auth, server_set)


def test_call_choice_is_not_available_for_legacy_single_server_contract():
    module = load_module()
    contract = load_contract()
    auth = authorization(contract, approvedCallChoices=[{"choiceId": "decision", "selection": "exactly-one", "candidates": []}])

    with pytest.raises(module.ContractError, match="require a pinned MCP server set"):
        module.validate_authorization(auth, contract)


def test_source_revisions_must_be_full_git_shas():
    module = load_module()
    contract_value = load_contract()
    with pytest.raises(module.ContractError, match="full lowercase Git SHA"):
        module.validate_authorization(authorization(contract_value, sourceRevision="7f65a22"), contract_value)