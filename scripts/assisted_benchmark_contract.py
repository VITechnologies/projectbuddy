#!/usr/bin/env python3
"""Validate the pinned LBA MCP surface and per-run assisted-benchmark authority."""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

DEFAULT_CONTRACT = Path(__file__).resolve().parents[1] / "tools" / "assisted-benchmark" / "lba-mcp-contract.json"
CONTRACT_SCHEMA = "projectbuddy/lba-mcp-contract@1"
SERVER_SET_SCHEMA = "projectbuddy/assisted-mcp-server-set@1"
AUTHORIZATION_SCHEMA = "projectbuddy/assisted-benchmark-authorization@1"
EFFECTS = {"read-only", "local-computation", "coordination-write", "plan-only"}


class ContractError(ValueError):
    pass


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ContractError("input is not readable valid JSON") from error


def load_contract(path: Path = DEFAULT_CONTRACT) -> dict:
    value = _read_json(path)
    validate_contract(value)
    return value


def _validate_single_contract(contract: object) -> None:
    if not isinstance(contract, dict) or contract.get("schema") != CONTRACT_SCHEMA:
        raise ContractError("unsupported contract schema")
    source = contract.get("source")
    if not isinstance(source, dict) or set(source) != {"repository", "revision", "serverName", "serverVersion", "protocolVersion"}:
        raise ContractError("contract source fields are invalid")
    if not all(isinstance(source[key], str) and source[key] for key in source):
        raise ContractError("contract source fields must be non-empty strings")
    if not re.fullmatch(r"[0-9a-f]{40}", source["revision"]):
        raise ContractError("contract source revision must be a full lowercase Git SHA")
    groups = contract.get("toolGroups")
    if not isinstance(groups, dict) or set(groups) != {"core", "acg-grid"}:
        raise ContractError("contract tool groups are invalid")
    names: set[str] = set()
    for group_name, group in groups.items():
        if not isinstance(group, dict) or set(group) != {"required", "tools"}:
            raise ContractError(f"tool group {group_name} is invalid")
        if type(group["required"]) is not bool or not isinstance(group["tools"], list):
            raise ContractError(f"tool group {group_name} has invalid required/tools fields")
        if group_name == "core" and group["required"] is not True:
            raise ContractError("core tool group must be required")
        if group_name == "acg-grid" and group["required"] is not False:
            raise ContractError("ACG grid tool group must remain optional")
        for tool in group["tools"]:
            if not isinstance(tool, dict) or set(tool) != {"name", "effect", "mutatesState", "requiresVmLifecycle", "inputSchema"}:
                raise ContractError(f"tool in {group_name} has invalid contract fields")
            name = tool["name"]
            if not isinstance(name, str) or not name or name in names:
                raise ContractError("tool names must be non-empty and unique")
            names.add(name)
            if tool["effect"] not in EFFECTS:
                raise ContractError(f"tool {name} has an unknown effect")
            if type(tool["mutatesState"]) is not bool or type(tool["requiresVmLifecycle"]) is not bool:
                raise ContractError(f"tool {name} has invalid policy flags")
            schema = tool["inputSchema"]
            if not isinstance(schema, dict) or schema.get("type") != "object" or schema.get("additionalProperties") is not False:
                raise ContractError(f"tool {name} input schema must be a closed object")
    if contract.get("authorizationSchema") != AUTHORIZATION_SCHEMA:
        raise ContractError("unsupported run authorization schema")
    evidence_fields = contract.get("allowedEvidenceFields")
    if not isinstance(evidence_fields, list) or not evidence_fields or any(not isinstance(item, str) or not item for item in evidence_fields):
        raise ContractError("allowed evidence fields are invalid")
    if len(set(evidence_fields)) != len(evidence_fields):
        raise ContractError("allowed evidence fields must be unique")


def validate_contract(contract: object) -> None:
    if isinstance(contract, dict) and contract.get("schema") == SERVER_SET_SCHEMA:
        expected_keys = {"schema", "sourceRevision", "authorizationSchema", "allowedEvidenceFields", "servers"}
        if set(contract) != expected_keys or not isinstance(contract["sourceRevision"], str) or not re.fullmatch(r"[0-9a-f]{40}", contract["sourceRevision"]):
            raise ContractError("MCP server-set fields or source revision are invalid")
        if contract["authorizationSchema"] != AUTHORIZATION_SCHEMA:
            raise ContractError("unsupported server-set authorization schema")
        servers = contract["servers"]
        if not isinstance(servers, dict) or len(servers) < 2:
            raise ContractError("MCP server set must contain at least two servers")
        identities: set[tuple[str, str, str]] = set()
        allowed_evidence: set[str] = set()
        for server_id, server_contract in servers.items():
            if not isinstance(server_id, str) or not re.fullmatch(r"[a-z][a-z0-9._-]{0,63}", server_id):
                raise ContractError("MCP server ID is invalid")
            _validate_single_contract(server_contract)
            source = server_contract["source"]
            identity = (source["serverName"], source["serverVersion"], source["revision"])
            if identity in identities:
                raise ContractError("MCP server identities must be unique")
            identities.add(identity)
            allowed_evidence.update(server_contract["allowedEvidenceFields"])
        evidence_fields = contract["allowedEvidenceFields"]
        if (not isinstance(evidence_fields, list) or not evidence_fields
                or any(not isinstance(item, str) or not item for item in evidence_fields)
                or len(set(evidence_fields)) != len(evidence_fields)
                or set(evidence_fields) != allowed_evidence):
            raise ContractError("server-set evidence fields must equal the unique union of server contracts")
        return
    _validate_single_contract(contract)


def tool_registry(contract: dict) -> dict[str, dict]:
    return {
        tool["name"]: {**tool, "group": group_name}
        for group_name, group in contract["toolGroups"].items()
        for tool in group["tools"]
    }


def tool_contract_for_call(contract: dict, tool_name: str, server_id: str | None = None) -> tuple[dict, dict]:
    if contract.get("schema") == SERVER_SET_SCHEMA:
        if not isinstance(server_id, str) or server_id not in contract["servers"]:
            raise ContractError("MCP server ID is not in the pinned server set")
        selected = contract["servers"][server_id]
    else:
        if server_id is not None and server_id != contract["source"]["serverName"]:
            raise ContractError("MCP server identity differs from the pinned contract")
        selected = contract
    tool = tool_registry(selected).get(tool_name)
    if tool is None:
        raise ContractError("unknown MCP tool in the pinned contract for that server")
    return selected, tool


def _validate_arguments(value: object, schema: dict, path: str) -> None:
    expected_type = schema.get("type")
    type_matches = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "boolean": lambda item: type(item) is bool,
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item),
    }
    if expected_type not in type_matches or not type_matches[expected_type](value):
        raise ContractError(f"tool arguments do not match the pinned schema at {path}")
    if "enum" in schema and value not in schema["enum"]:
        raise ContractError(f"tool argument is outside the pinned enum at {path}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ContractError(f"tool argument is below the pinned minimum at {path}")
        if "maximum" in schema and value > schema["maximum"]:
            raise ContractError(f"tool argument exceeds the pinned maximum at {path}")
    if expected_type == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if any(key not in value for key in required):
            raise ContractError(f"tool arguments are missing a required field at {path}")
        if schema.get("additionalProperties") is False and any(key not in properties for key in value):
            raise ContractError(f"tool arguments contain an unpinned field at {path}")
        for key, child in value.items():
            if key in properties:
                _validate_arguments(child, properties[key], f"{path}.{key}")
    if expected_type == "array":
        item_schema = schema.get("items")
        if item_schema is not None:
            for index, item in enumerate(value):
                _validate_arguments(item, item_schema, f"{path}[{index}]")


def validate_snapshot(snapshot: object, contract: dict) -> dict:
    validate_contract(contract)
    if contract["schema"] == SERVER_SET_SCHEMA:
        raise ContractError("validate server-set capabilities with validate_server_snapshot")
    if not isinstance(snapshot, dict) or set(snapshot) != {"protocolVersion", "serverInfo", "tools"}:
        raise ContractError("capability snapshot must contain protocolVersion, serverInfo, and tools")
    source = contract["source"]
    if snapshot["protocolVersion"] != source["protocolVersion"]:
        raise ContractError("MCP protocol version mismatch")
    server = snapshot["serverInfo"]
    if not isinstance(server, dict) or server.get("name") != source["serverName"] or server.get("version") != source["serverVersion"]:
        raise ContractError("MCP server identity/version mismatch")
    tools = snapshot["tools"]
    if not isinstance(tools, list):
        raise ContractError("MCP tools/list result must contain an array")
    registry = tool_registry(contract)
    observed: dict[str, dict] = {}
    for tool in tools:
        if not isinstance(tool, dict):
            raise ContractError("MCP tool entry must be an object")
        name = tool.get("name")
        if not isinstance(name, str) or name not in registry:
            raise ContractError("MCP tool list contains an unknown tool")
        if name in observed:
            raise ContractError("MCP tool list contains a duplicate tool")
        if not isinstance(tool.get("description"), str) or not isinstance(tool.get("inputSchema"), dict):
            raise ContractError(f"MCP tool {name} is missing description or inputSchema")
        if tool["inputSchema"] != registry[name]["inputSchema"]:
            raise ContractError(f"MCP tool {name} input schema drifted")
        observed[name] = tool

    available_groups: list[str] = []
    missing_optional_groups: list[str] = []
    for group_name, group in contract["toolGroups"].items():
        expected_names = {tool["name"] for tool in group["tools"]}
        present_names = expected_names.intersection(observed)
        if not group["required"] and not present_names:
            missing_optional_groups.append(group_name)
            continue
        if present_names != expected_names:
            label = "core tool group" if group_name == "core" else "ACG grid tool group"
            raise ContractError(f"{label} is incomplete")
        available_groups.append(group_name)
    return {
        "contractSchema": contract["schema"],
        "serverName": source["serverName"],
        "serverVersion": source["serverVersion"],
        "protocolVersion": source["protocolVersion"],
        "toolCount": len(observed),
        "availableGroups": available_groups,
        "missingOptionalGroups": missing_optional_groups,
    }


def validate_server_snapshot(snapshot: object, server_id: str, server_set: dict) -> dict:
    validate_contract(server_set)
    if server_set.get("schema") != SERVER_SET_SCHEMA or server_id not in server_set["servers"]:
        raise ContractError("MCP server ID is not in the pinned server set")
    result = validate_snapshot(snapshot, server_set["servers"][server_id])
    return {**result, "serverId": server_id}


def validate_review_execution(policy: object, model_digest: str) -> dict:
    if not isinstance(policy, dict) or set(policy) != {"schema", "modelDigest", "mode", "threads"}:
        raise ContractError("review execution policy fields are invalid")
    if (policy["schema"] != "projectbuddy/ollama-execution@1" or policy["mode"] != "cpu-only"
            or not isinstance(policy["modelDigest"], str) or not re.fullmatch(r"[a-f0-9]{64}", policy["modelDigest"])
            or policy["modelDigest"] != model_digest or type(policy["threads"]) is not int or not 1 <= policy["threads"] <= 8):
        raise ContractError("review execution policy is unsupported or differs from the approved model")
    return dict(policy)


def validate_review_lifecycle(policy: object) -> dict:
    keys = {"schema", "totalSeconds", "requestSeconds", "maxRequests", "maxRetries", "backoffSeconds",
            "residencySeconds", "maxImageCount"}
    if not isinstance(policy, dict) or set(policy) != keys or policy["schema"] != "projectbuddy/ollama-lifecycle@1":
        raise ContractError("review lifecycle policy fields are invalid")
    for key, low, high in (("totalSeconds", 1, 900), ("requestSeconds", 1, 120), ("backoffSeconds", 0, 5)):
        value = policy[key]
        if type(value) not in {int, float} or not math.isfinite(value) or not low <= value <= high:
            raise ContractError("review lifecycle time bounds are invalid")
    for key, low, high in (("maxRequests", 1, 128), ("maxRetries", 0, 3), ("residencySeconds", 1, 300), ("maxImageCount", 0, 3)):
        if type(policy[key]) is not int or not low <= policy[key] <= high:
            raise ContractError("review lifecycle count/residency bounds are invalid")
    if policy["requestSeconds"] > policy["totalSeconds"]:
        raise ContractError("review lifecycle request deadline exceeds overall deadline")
    return dict(policy)


def validate_review_privacy(policy: object) -> dict:
    keys = {"schema", "imageMode", "frames", "retainModelText", "expiresAt"}
    if not isinstance(policy, dict) or set(policy) != keys:
        raise ContractError("review privacy policy fields are invalid")
    if (policy["schema"] != "projectbuddy/ollama-privacy@1"
            or policy["imageMode"] not in {"none", "full-frame", "regions"}
            or policy["retainModelText"] is not False):
        raise ContractError("review privacy policy mode is unsupported")
    try:
        expiry = datetime.fromisoformat(policy["expiresAt"].replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        raise ContractError("review privacy expiry is invalid") from None
    if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
        raise ContractError("review privacy policy expired or has no timezone; reuse refused, cleanup requires authorization")
    frames = policy["frames"]
    if (not isinstance(frames, list) or len(frames) > 3
            or (policy["imageMode"] == "regions") != bool(frames)):
        raise ContractError("review privacy regions are invalid")
    indexes = set()
    for frame in frames:
        if (not isinstance(frame, dict) or set(frame) != {"index", "crop", "redactions"}
                or type(frame["index"]) is not int or frame["index"] < 0 or frame["index"] in indexes
                or not isinstance(frame["redactions"], list) or len(frame["redactions"]) > 128):
            raise ContractError("review privacy frame policy is invalid")
        indexes.add(frame["index"])
        for box in [frame["crop"], *frame["redactions"]]:
            if (not isinstance(box, list) or len(box) != 4 or any(type(value) is not int for value in box)
                    or box[0] < 0 or box[1] < 0 or box[2] <= 0 or box[3] <= 0):
                raise ContractError("review privacy region bounds are invalid")
    return json.loads(json.dumps(policy))


def validate_review_budget(budget: object, model_digest: str) -> dict:
    keys = {"schema", "accounting", "modelDigest", "contextTokens", "outputTokens", "templateReserveTokens",
            "imageTokensPerImage", "maxTextBytes", "maxImageBytes", "maxImagesPerRequest"}
    if not isinstance(budget, dict) or set(budget) != keys:
        raise ContractError("review budget fields are invalid")
    if (budget["schema"] != "projectbuddy/ollama-budget@1"
            or budget["accounting"] != "utf8-byte-plus-approved-media@1"):
        raise ContractError("review budget accounting policy is unsupported")
    if (not isinstance(budget["modelDigest"], str) or not re.fullmatch(r"[0-9a-f]{64}", budget["modelDigest"])
            or budget["modelDigest"] != model_digest):
        raise ContractError("review budget does not identify the approved model digest")
    bounds = {"contextTokens": (1, 32768), "outputTokens": (1, 4096), "templateReserveTokens": (256, 16384),
              "imageTokensPerImage": (0, 32768), "maxTextBytes": (1, 512 * 1024),
              "maxImageBytes": (0, 8 * 1024 * 1024), "maxImagesPerRequest": (0, 1)}
    for key, (minimum, maximum) in bounds.items():
        value = budget[key]
        if type(value) is not int or not minimum <= value <= maximum:
            raise ContractError(f"review budget {key} is invalid")
    if budget["outputTokens"] + budget["templateReserveTokens"] >= budget["contextTokens"]:
        raise ContractError("review budget has no input capacity")
    if budget["maxImagesPerRequest"] and (not budget["imageTokensPerImage"] or not budget["maxImageBytes"]):
        raise ContractError("review budget requires an approved model-specific image bound")
    return dict(budget)


def validate_authorization(authorization: object, contract: dict) -> dict:
    validate_contract(contract)
    required = {
        "schema", "runId", "workload", "sourceRevision", "approvedBy", "approvedAt",
        "approvedCalls", "targetVmUuids", "evidenceFields", "allowVmLifecycle",
        "teardownOnNativePplFailure", "ollamaModel",
    }
    if (not isinstance(authorization, dict) or not required.issubset(authorization)
            or set(authorization) - required - {"reviewBudget", "reviewPrivacy", "reviewLifecycle", "reviewExecution", "approvedCallChoices"}):
        raise ContractError("run authorization fields are invalid")
    if authorization["schema"] != AUTHORIZATION_SCHEMA:
        raise ContractError("unsupported run authorization schema")
    for key in ("runId", "workload", "sourceRevision", "approvedBy", "approvedAt"):
        if not isinstance(authorization[key], str) or not authorization[key].strip():
            raise ContractError(f"run authorization {key} must be a non-empty string")
    if not re.fullmatch(r"[0-9a-f]{40}", authorization["sourceRevision"]):
        raise ContractError("run authorization sourceRevision must be a full lowercase Git SHA")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", authorization["runId"]):
        raise ContractError("run authorization runId has invalid characters")
    try:
        timestamp = datetime.fromisoformat(authorization["approvedAt"].replace("Z", "+00:00"))
    except ValueError as error:
        raise ContractError("run authorization approvedAt must be an ISO-8601 timestamp") from error
    if timestamp.tzinfo is None:
        raise ContractError("run authorization approvedAt must include a timezone")
    model = authorization["ollamaModel"]
    if not isinstance(model, dict) or set(model) != {"tag", "digest"}:
        raise ContractError("run authorization ollamaModel must contain tag and digest")
    if not isinstance(model["tag"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*:[A-Za-z0-9][A-Za-z0-9._-]*", model["tag"]):
        raise ContractError("run authorization Ollama model tag is invalid")
    if not isinstance(model["digest"], str) or not re.fullmatch(r"[0-9a-fA-F]{64}", model["digest"]):
        raise ContractError("run authorization Ollama model digest must be SHA-256")
    server_set = contract.get("schema") == SERVER_SET_SCHEMA
    approved_calls = authorization["approvedCalls"]
    if not isinstance(approved_calls, list) or not approved_calls:
        raise ContractError("run authorization approvedCalls must be a non-empty array")
    canonical_calls: list[dict] = []
    call_keys: set[str] = set()
    for call in approved_calls:
        expected_call_keys = {"serverId", "tool", "arguments"} if server_set else {"tool", "arguments"}
        if not isinstance(call, dict) or set(call) != expected_call_keys or not isinstance(call["tool"], str):
            raise ContractError("approved MCP call fields do not match the pinned contract mode")
        server_id = call.get("serverId")
        selected_contract, tool = tool_contract_for_call(contract, call["tool"], server_id)
        _validate_arguments(call["arguments"], tool["inputSchema"], call["tool"])
        normalized_call = {"tool": call["tool"], "arguments": call["arguments"]}
        if server_set:
            normalized_call["serverId"] = server_id
        key = json.dumps(normalized_call, sort_keys=True, separators=(",", ":"))
        if key in call_keys:
            raise ContractError("run authorization contains a duplicate MCP call")
        call_keys.add(key)
        canonical_calls.append(normalized_call)
    vm_uuids = authorization["targetVmUuids"]
    if not isinstance(vm_uuids, list) or any(not isinstance(value, str) for value in vm_uuids):
        raise ContractError("run authorization targetVmUuids must be a string array")
    canonical_uuids: list[str] = []
    try:
        canonical_uuids = [str(UUID(value)) for value in vm_uuids]
    except ValueError as error:
        raise ContractError("run authorization contains an invalid VM UUID") from error
    if len(set(canonical_uuids)) != len(canonical_uuids):
        raise ContractError("run authorization targetVmUuids must be unique")
    if type(authorization["allowVmLifecycle"]) is not bool or type(authorization["teardownOnNativePplFailure"]) is not bool:
        raise ContractError("run authorization lifecycle permissions must be booleans")
    if authorization["allowVmLifecycle"] and not canonical_uuids:
        raise ContractError("VM lifecycle authorization requires a target VM UUID")
    if authorization["teardownOnNativePplFailure"] and (not authorization["allowVmLifecycle"] or len(canonical_uuids) != 1):
        raise ContractError("PPL-failure teardown authorization requires exactly one approved VM UUID")
    evidence_fields = authorization["evidenceFields"]
    allowed_evidence = set(contract["allowedEvidenceFields"])
    if not isinstance(evidence_fields, list) or not evidence_fields or any(not isinstance(field, str) for field in evidence_fields):
        raise ContractError("run authorization evidenceFields must be a non-empty string array")
    if len(set(evidence_fields)) != len(evidence_fields) or any(field not in allowed_evidence for field in evidence_fields):
        raise ContractError("run authorization contains duplicate or unknown evidence fields")
    normalized = dict(authorization)
    normalized["approvedCalls"] = canonical_calls
    normalized["targetVmUuids"] = canonical_uuids
    normalized["ollamaModel"] = {"tag": model["tag"], "digest": model["digest"].lower()}
    if "approvedCallChoices" in authorization:
        choice_groups = authorization["approvedCallChoices"]
        if not isinstance(choice_groups, list) or len(choice_groups) > 1:
            raise ContractError("run authorization must contain at most one call choice group")
        if choice_groups:
            if not server_set:
                raise ContractError("call choice groups require a pinned MCP server set")
            group = choice_groups[0]
            if (not isinstance(group, dict) or set(group) != {"choiceId", "selection", "candidates"}
                    or not isinstance(group["choiceId"], str)
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", group["choiceId"])
                    or group["selection"] != "exactly-one"
                    or not isinstance(group["candidates"], list) or len(group["candidates"]) != 2):
                raise ContractError("call choice group must contain exactly two candidates and one selection")
            normalized_candidates: list[dict] = []
            candidate_keys: set[str] = set()
            candidate_servers: set[str] = set()
            candidate_tools: set[str] = set()
            for candidate in group["candidates"]:
                if (not isinstance(candidate, dict) or set(candidate) != {"serverId", "tool", "arguments"}
                        or not isinstance(candidate["tool"], str)):
                    raise ContractError("call choice candidate fields are invalid")
                server_id = candidate["serverId"]
                selected_contract, tool = tool_contract_for_call(contract, candidate["tool"], server_id)
                _validate_arguments(candidate["arguments"], tool["inputSchema"], candidate["tool"])
                normalized_candidate = {
                    "serverId": server_id,
                    "tool": candidate["tool"],
                    "arguments": candidate["arguments"],
                }
                key = json.dumps(normalized_candidate, sort_keys=True, separators=(",", ":"))
                if key in candidate_keys or key in call_keys:
                    raise ContractError("call choice contains duplicate or already approved candidate")
                candidate_keys.add(key)
                candidate_servers.add(server_id)
                candidate_tools.add(candidate["tool"])
                normalized_candidates.append(normalized_candidate)
            if len(candidate_servers) != 1 or len(candidate_tools) != 1:
                raise ContractError("call choice candidates must use one exact server and tool")
            normalized["approvedCallChoices"] = [{
                "choiceId": group["choiceId"],
                "selection": "exactly-one",
                "candidates": normalized_candidates,
            }]
    if "reviewBudget" in authorization:
        normalized["reviewBudget"] = validate_review_budget(authorization["reviewBudget"], normalized["ollamaModel"]["digest"])
    if "reviewPrivacy" in authorization:
        normalized["reviewPrivacy"] = validate_review_privacy(authorization["reviewPrivacy"])
    if "reviewLifecycle" in authorization:
        normalized["reviewLifecycle"] = validate_review_lifecycle(authorization["reviewLifecycle"])
    if "reviewExecution" in authorization:
        normalized["reviewExecution"] = validate_review_execution(authorization["reviewExecution"], normalized["ollamaModel"]["digest"])
    return normalized


def authorize_tool_call(tool_name: str, arguments: object, authorization: object, contract: dict,
                        server_id: str | None = None) -> dict:
    approved = validate_authorization(authorization, contract)
    server_set = contract.get("schema") == SERVER_SET_SCHEMA
    selected_contract, tool = tool_contract_for_call(contract, tool_name, server_id)
    _validate_arguments(arguments, tool["inputSchema"], tool_name)
    approved_call = {"tool": tool_name, "arguments": arguments}
    if server_set:
        approved_call["serverId"] = server_id
    approved_key = json.dumps(approved_call, sort_keys=True, separators=(",", ":"))
    approved_keys = {
        json.dumps(call, sort_keys=True, separators=(",", ":"))
        for call in approved["approvedCalls"]
    }
    for group in approved.get("approvedCallChoices", []):
        approved_keys.update(
            json.dumps(candidate, sort_keys=True, separators=(",", ":"))
            for candidate in group["candidates"]
        )
    if approved_key not in approved_keys:
        raise ContractError("requested MCP call is outside the per-run approval")
    if tool["requiresVmLifecycle"] and not approved["allowVmLifecycle"]:
        raise ContractError("requested MCP tool requires VM lifecycle approval")
    decision = {
        "allowed": True,
        "runId": approved["runId"],
        "tool": tool_name,
        "ollamaModel": approved["ollamaModel"],
        "effect": tool["effect"],
        "mutatesState": tool["mutatesState"],
        "executesMcpResultCommands": False,
    }
    if server_set:
        decision["serverId"] = server_id
    return decision


def authorize_ppl_failure_cleanup(authorization: object, vm_uuid: str, contract: dict) -> dict:
    approved = validate_authorization(authorization, contract)
    try:
        exact_uuid = str(UUID(vm_uuid))
    except ValueError as error:
        raise ContractError("cleanup target must be a valid VM UUID") from error
    if not approved["allowVmLifecycle"] or not approved["teardownOnNativePplFailure"]:
        raise ContractError("run approval does not authorize PPL-failure teardown")
    if approved["targetVmUuids"] != [exact_uuid]:
        raise ContractError("PPL-failure teardown target does not match the single approved VM UUID")
    return {"runId": approved["runId"], "vmUuid": exact_uuid, "authorized": True}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate-snapshot")
    validate.add_argument("snapshot", type=Path)
    validate.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    authorize = subparsers.add_parser("authorize-tool")
    authorize.add_argument("authorization", type=Path)
    authorize.add_argument("tool")
    authorize.add_argument("arguments", type=Path, help="JSON file containing the exact approved arguments")
    authorize.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    cleanup = subparsers.add_parser("authorize-ppl-failure-cleanup")
    cleanup.add_argument("authorization", type=Path)
    cleanup.add_argument("vm_uuid")
    cleanup.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    args = parser.parse_args(argv)
    try:
        contract = load_contract(args.contract)
        if args.command == "validate-snapshot":
            result = validate_snapshot(_read_json(args.snapshot), contract)
        elif args.command == "authorize-tool":
            result = authorize_tool_call(args.tool, _read_json(args.arguments), _read_json(args.authorization), contract)
        else:
            result = authorize_ppl_failure_cleanup(_read_json(args.authorization), args.vm_uuid, contract)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except ContractError as error:
        print(f"assisted-benchmark-contract: refused: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())