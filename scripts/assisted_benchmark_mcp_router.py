"""Route already-authorized MCP calls to their pinned server clients."""
from __future__ import annotations

import inspect
import json
from datetime import datetime, timezone
from types import MappingProxyType

from assisted_benchmark_contract import (
    SERVER_SET_SCHEMA,
    ContractError,
    tool_contract_for_call,
    validate_contract,
    validate_authorization,
    validate_server_snapshot,
)
from assisted_benchmark_live_policy import canonical_json, sha256


class McpRouterError(ValueError):
    """A routed MCP call does not match the pinned server set."""


class PinnedMcpRouter:
    def __init__(self, server_set: dict, authorization: dict, snapshots: dict, clients: dict, clock=None):
        if not isinstance(server_set, dict) or server_set.get("schema") != SERVER_SET_SCHEMA:
            raise McpRouterError("router requires a pinned MCP server set")
        try:
            validate_contract(server_set)
            validated_authorization = validate_authorization(authorization, server_set)
        except ContractError as error:
            raise McpRouterError("MCP server set or authorization is invalid") from error
        if (server_set.get("schema") != SERVER_SET_SCHEMA
                or not isinstance(snapshots, dict) or not isinstance(clients, dict)
                or set(snapshots) != set(server_set["servers"])
                or set(clients) != set(server_set["servers"])):
            raise McpRouterError("MCP server clients and snapshots must exactly match the pinned server set")
        for server_id, snapshot in snapshots.items():
            try:
                validate_server_snapshot(snapshot, server_id, server_set)
            except ContractError as error:
                raise McpRouterError("MCP server capability snapshot differs from its pinned identity") from error
            if not callable(getattr(clients[server_id], "call_tool", None)):
                raise McpRouterError("MCP server client lacks the required call adapter")
        self._server_set = server_set
        self._contract_sha256 = sha256(server_set)
        self._authorization_bytes = canonical_json(validated_authorization["approvedCalls"])
        self._choice_bytes = canonical_json(validated_authorization.get("approvedCallChoices", []))
        self._run_id = validated_authorization["runId"]
        self._vm_uuids = frozenset(validated_authorization["targetVmUuids"])
        self._next_index = 0
        self._choice_selected = False
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._clients = MappingProxyType(dict(clients))

    async def dispatch(self, decision: dict):
        """Consume one exact grant decision; ambiguous dispatches are never retried."""
        if not isinstance(decision, dict) or decision.get("allowed") is not True:
            raise McpRouterError("MCP dispatch requires an allowed broker decision")
        server_id = decision.get("serverId")
        if not isinstance(server_id, str) or server_id not in self._clients:
            raise McpRouterError("MCP decision names an unknown pinned server")
        server_contract = self._server_set["servers"][server_id]
        if (decision.get("serverIdentity") != server_contract["source"]
                or decision.get("contractSha256") != self._contract_sha256):
            raise McpRouterError("MCP decision server identity or contract digest changed")
        try:
            expires_at = datetime.fromisoformat(decision["expiresAt"].replace("Z", "+00:00"))
            current_time = self._clock()
        except (AttributeError, KeyError, TypeError, ValueError):
            raise McpRouterError("MCP decision expiry is invalid") from None
        if (expires_at.tzinfo is None or current_time.tzinfo is None
                or current_time.astimezone(timezone.utc) >= expires_at.astimezone(timezone.utc)):
            raise McpRouterError("MCP decision expired before client dispatch")
        calls = json.loads(self._authorization_bytes)
        choice_groups = json.loads(self._choice_bytes)
        choice_group = None
        if self._next_index < len(calls):
            expected_call = calls[self._next_index]
            if "choiceId" in decision:
                raise McpRouterError("MCP decision names an unselected call choice")
        else:
            if self._choice_selected or not choice_groups:
                raise McpRouterError("MCP authorization sequence is exhausted")
            choice_group = choice_groups[0]
            if decision.get("choiceId") != choice_group["choiceId"]:
                raise McpRouterError("MCP decision does not identify the sealed choice group")
            expected_call = None
        proposed_call = {
            "serverId": server_id,
            "tool": decision.get("tool"),
            "arguments": decision.get("arguments"),
        }
        expected_matches = (
            [expected_call] if expected_call is not None else choice_group["candidates"]
        )
        if (decision.get("runId") != self._run_id
            or decision.get("vmUuid") not in self._vm_uuids
            or decision.get("callIndex") != self._next_index
            or not any(canonical_json(proposed_call) == canonical_json(item) for item in expected_matches)):
            raise McpRouterError("MCP decision differs from the next exact grant call")
        tool_name = decision.get("tool")
        arguments = decision.get("arguments")
        try:
            selected_contract, tool = tool_contract_for_call(self._server_set, tool_name, server_id)
        except (ContractError, TypeError) as error:
            raise McpRouterError("MCP decision tool is not in the pinned server contract") from error
        if (selected_contract is not server_contract
                or decision.get("effect") != tool["effect"]
                or decision.get("mutatesState") is not tool["mutatesState"]
                or decision.get("executesMcpResultCommands") is not False):
            raise McpRouterError("MCP decision effect differs from the pinned server tool")
        if choice_group is not None:
            self._choice_selected = True
        else:
            self._next_index += 1
        client = self._clients[server_id]
        result = client.call_tool(tool_name, arguments)
        if inspect.isawaitable(result):
            result = await result
        if getattr(result, "is_error", getattr(result, "isError", False)) is True:
            raise McpRouterError("pinned MCP client rejected the authorized call")
        return result