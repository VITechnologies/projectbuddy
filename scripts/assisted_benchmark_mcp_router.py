"""Route already-authorized MCP calls to their pinned server clients."""
from __future__ import annotations

import inspect
import json
from datetime import datetime, timezone
from types import MappingProxyType
from uuid import UUID

from assisted_benchmark_contract import (
    SERVER_SET_SCHEMA,
    ContractError,
    tool_contract_for_call,
    validate_contract,
    validate_authorization,
    validate_server_snapshot,
)
from assisted_benchmark_live_policy import LiveRunBroker, canonical_json, sha256


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
        self._server_set = json.loads(canonical_json(server_set))
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
        if ((isinstance(result, dict) and result.get("isError", result.get("is_error", False)) is True)
            or getattr(result, "is_error", getattr(result, "isError", False)) is True):
            raise McpRouterError("pinned MCP client rejected the authorized call")
        return result


class LiveChoiceDispatcher:
    """Consume one sealed UI choice after a verified capture-time selection."""

    def __init__(self, broker: LiveRunBroker, router: PinnedMcpRouter, binding: dict,
                 actor_server_id: str, ui_server_id: str):
        from assisted_benchmark_active_capture import RESOURCE_CONTRACT

        if not isinstance(broker, LiveRunBroker) or not isinstance(router, PinnedMcpRouter):
            raise McpRouterError("live choice requires the sealed broker and pinned router")
        grant = broker.grant
        contract = broker.contract
        if (contract.get("schema") != SERVER_SET_SCHEMA or actor_server_id not in contract["servers"]
                or ui_server_id not in contract["servers"] or actor_server_id == ui_server_id
                or router._contract_sha256 != grant["contractSha256"]
                or router._run_id != grant["runId"]
                or router._authorization_bytes != canonical_json(broker.authorization["approvedCalls"])
                or router._choice_bytes != canonical_json(broker.authorization.get("approvedCallChoices", []))):
            raise McpRouterError("live choice server set differs from the sealed grant")
        actor = contract["servers"][actor_server_id]
        if (not isinstance(binding, dict) or set(binding) != {"schema", "grantRunId", "vmUuid", "captureRunId", "actorSource", "contractSha256"}
                or binding["schema"] != "projectbuddy/active-capture-binding@1"
                or binding["grantRunId"] != grant["runId"] or binding["vmUuid"] != grant["vmUuid"]
                or binding["actorSource"] != actor["source"] or binding["contractSha256"] != sha256(actor)
                or actor.get("captureResources") != RESOURCE_CONTRACT):
            raise McpRouterError("live choice capture binding differs from the sealed actor and VM")
        try:
            if str(UUID(binding["captureRunId"])) != binding["captureRunId"]:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise McpRouterError("live choice capture ID is not an opaque canonical UUID") from None
        groups = broker.authorization.get("approvedCallChoices", [])
        if len(groups) != 1 or groups[0].get("selection") != "exactly-one":
            raise McpRouterError("live choice requires one exact sealed choice group")
        candidates = groups[0]["candidates"]
        if (len(candidates) != 2 or any(candidate["serverId"] != ui_server_id or candidate["tool"] != "click_target"
                                      or set(candidate["arguments"]) != {"taskId", "targetId"} for candidate in candidates)
                or {candidate["arguments"]["targetId"] for candidate in candidates} != {"approve", "hold"}
                or len({candidate["arguments"]["taskId"] for candidate in candidates}) != 1):
            raise McpRouterError("live choice must name exactly the two approved synthetic UI actions")
        self._binding_bytes = canonical_json(binding)
        self._candidate_bytes = canonical_json(candidates)
        self._broker = broker
        self._router = router
        self._applied = False
        self._failed = False
        self._attempted = False

    @property
    def binding(self) -> dict:
        return json.loads(self._binding_bytes)

    @property
    def applied(self) -> bool:
        return self._applied

    @property
    def outcome_unknown(self) -> bool:
        return self._attempted and not self._applied

    def abort(self) -> None:
        self._failed = True
        self._broker.abort()

    async def dispatch_selection(self, choice: dict, context: dict) -> bool:
        if self._failed:
            raise McpRouterError("live choice dispatcher is terminal")
        if self._applied:
            return False
        try:
            binding = self.binding
            if sha256(self._broker.contract) != self._broker.grant["contractSha256"]:
                raise McpRouterError("live choice contract changed after grant sealing")
            if (not isinstance(context, dict) or set(context) != {"binding", "captureState", "frameSequence", "modelDigest"}
                    or canonical_json(context["binding"]) != self._binding_bytes or context["captureState"] != "capturing"
                    or type(context["frameSequence"]) is not int or context["frameSequence"] < 1
                    or context["modelDigest"] != self._broker.authorization["ollamaModel"]["digest"]
                    or not isinstance(choice, dict) or set(choice) != {"action", "captureRunId", "frameId"}
                    or choice["captureRunId"] != binding["captureRunId"]
                    or choice["frameId"] != f"{binding['captureRunId']}:{context['frameSequence']}"):
                raise McpRouterError("live choice is not bound to current verified capture evidence")
            candidate = next((item for item in json.loads(self._candidate_bytes)
                              if item["arguments"]["targetId"] == choice["action"]), None)
            if candidate is None:
                raise McpRouterError("live choice is outside the sealed action candidates")
            decision = self._broker.begin_call(candidate["tool"], candidate["arguments"], binding["vmUuid"], candidate["serverId"])
            self._attempted = True
            result = await self._router.dispatch(decision)
            acknowledgement = (result.get("structuredContent") if isinstance(result, dict)
                               else getattr(result, "structured_content", getattr(result, "structuredContent", None)))
            task_id = candidate["arguments"]["taskId"]
            if (not isinstance(acknowledgement, dict) or set(acknowledgement) != {"accepted", "taskId", "state", "eventId"}
                    or acknowledgement["accepted"] is not True or acknowledgement["taskId"] != task_id
                    or acknowledgement["state"] != "decision_recorded" or acknowledgement["eventId"] != f"{task_id}-event-0001"):
                raise McpRouterError("live choice acknowledgement is incomplete or belongs to another task")
            self._broker.complete_call(True)
            self._applied = True
            return True
        except BaseException:
            self.abort()
            raise