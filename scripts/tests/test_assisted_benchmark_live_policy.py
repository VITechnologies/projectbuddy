from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import tempfile
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY / "scripts"))

import assisted_benchmark_live_policy as live_policy
from assisted_benchmark_synthetic_ui_mcp_server import (
    MCP_SERVER_NAME,
    MCP_SERVER_VERSION,
    UI_TOOL_INPUT_SCHEMA,
)
derive_host_identity_sha256 = live_policy.host_identity_sha256
get_host_ledger_directory = live_policy._live_run_ledger_directory

CONTRACT_PATH = REPOSITORY / "tools" / "assisted-benchmark" / "lba-mcp-contract.json"
RUN_ID = "11111111-2222-4333-8444-555555555555"
EXISTING_VM = "00000000-0000-4000-8000-000000000001"
CREATED_VM = "00000000-0000-4000-8000-000000000002"
NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
MODEL_DIGEST = "845dbda0ea48ed749caafd9e6037047aa19acfcfd82e704d7ca97d631a0b697e"
HOST_IDENTITY = "b" * 64


@pytest.fixture(autouse=True)
def fake_host_identity(monkeypatch, tmp_path):
    monkeypatch.setattr(live_policy, "host_identity_sha256", lambda: HOST_IDENTITY)
    ledger = tmp_path / "ledger"
    ledger.mkdir(mode=0o700)
    ledger.chmod(0o700)
    monkeypatch.setattr(live_policy, "_live_run_ledger_directory", lambda: ledger)


def test_host_identity_digest_is_domain_separated_and_does_not_return_raw_id(tmp_path):
    raw_id = b"0123456789abcdef0123456789abcdef"
    identity_file = tmp_path / "machine-id"
    identity_file.write_bytes(raw_id + b"\n")
    digest = derive_host_identity_sha256(identity_file)
    expected = hashlib.sha256(b"projectbuddy/host-identity@1\0" + raw_id).hexdigest()
    assert digest == expected
    assert digest != raw_id.decode()
    alias = tmp_path / "machine-id-link"
    alias.symlink_to(identity_file)
    with pytest.raises(live_policy.LivePolicyError, match="identity source is unsupported|unavailable"):
        derive_host_identity_sha256(alias)


@pytest.mark.skipif(os.name != "posix", reason="host ledger uses the POSIX account database")
def test_persistent_host_ledger_ignores_home_environment(monkeypatch, tmp_path):
    import pwd

    expected = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local" / "state" / "projectbuddy" / "live-run-ledger"
    monkeypatch.setenv("HOME", str(tmp_path / "attacker-selected-home"))
    assert get_host_ledger_directory() == expected


def contract():
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))

def server_set_contract(ui_protocol_version="2025-06-18"):
    lba_contract = contract()
    ui_contract = copy.deepcopy(lba_contract)
    ui_contract["source"] = {
        "repository": "VITechnologies/projectbuddy",
        "revision": "a" * 40,
        "serverName": MCP_SERVER_NAME,
        "serverVersion": MCP_SERVER_VERSION,
        "protocolVersion": ui_protocol_version,
    }
    ui_contract["toolGroups"] = {
        "core": {
            "required": True,
            "tools": [
                {
                    "name": "click_target",
                    "effect": "coordination-write",
                    "mutatesState": True,
                    "requiresVmLifecycle": False,
                    "inputSchema": copy.deepcopy(UI_TOOL_INPUT_SCHEMA),
                }
            ],
        },
        "acg-grid": {"required": False, "tools": []},
    }
    return {
        "schema": "projectbuddy/assisted-mcp-server-set@1",
        "sourceRevision": lba_contract["source"]["revision"],
        "authorizationSchema": lba_contract["authorizationSchema"],
        "allowedEvidenceFields": lba_contract["allowedEvidenceFields"],
        "servers": {"lba": lba_contract, "synthetic-ui": ui_contract},
    }


def trusted_digest(value=None):
    return live_policy.sha256(policy() if value is None else value)


def policy():
    return {
        "schema": live_policy.POLICY_SCHEMA,
        "policyId": "synthetic-live-ui-v1",
        "sourceRevision": "7f65a2215b952e1f31bc3677087f14c8b528d789",
        "provisioning": {
            "hostIdentitySha256": HOST_IDENTITY,
            "hostProfile": "current-operator-host",
            "guestOs": "ubuntu",
            "baseImageSha256": "a" * 64,
            "vmNamePrefix": "live-ui",
            "vcpus": 2,
            "memoryMb": 4096,
            "diskGb": 40,
            "maxVms": 1,
        },
        "authorizationTemplate": {
            "workload": "synthetic-ui-live",
            "approvedCalls": [
                {"tool": "get_host_capabilities", "arguments": {}},
                {"tool": "get_benchmark_series", "arguments": {}},
            ],
            "evidenceFields": ["captureMetadata", "frameHashes", "resourceSamples"],
            "ollamaModel": {"tag": "qwen2.5:7b", "digest": MODEL_DIGEST},
            "reviewExecution": {"schema": "projectbuddy/ollama-execution@1", "modelDigest": MODEL_DIGEST,
                                "mode": "cpu-only", "threads": 1},
            "reviewBudget": {"schema": "projectbuddy/ollama-budget@1", "accounting": "utf8-byte-plus-approved-media@1",
                             "modelDigest": MODEL_DIGEST, "contextTokens": 8192, "outputTokens": 256,
                             "templateReserveTokens": 1024, "imageTokensPerImage": 0,
                             "maxTextBytes": 65536, "maxImageBytes": 0, "maxImagesPerRequest": 0},
            "reviewPrivacy": {"schema": "projectbuddy/ollama-privacy@1", "imageMode": "none", "frames": [],
                              "retainModelText": False, "expiresAt": "2030-01-02T00:00:00Z"},
            "reviewLifecycle": {"schema": "projectbuddy/ollama-lifecycle@1", "totalSeconds": 120,
                                "requestSeconds": 60, "maxRequests": 8, "maxRetries": 0,
                                "backoffSeconds": 0, "residencySeconds": 30, "maxImageCount": 0},
        },
    }


def permit_for(value=None, contract_value=None, inventory=None, now=NOW, run_id=RUN_ID):
    selected_policy = policy() if value is None else value
    selected_contract = contract() if contract_value is None else contract_value
    with live_policy.ProvisioningLock() as host_lock:
        return live_policy.compile_provision_permit(
            selected_policy, selected_contract, run_id,
            [EXISTING_VM] if inventory is None else inventory,
            trusted_digest(selected_policy), host_lock, now)


def grant_for(value=None, contract_value=None, permit=None, after=None, returned=CREATED_VM, now=NOW, run_id=RUN_ID):
    selected_policy = policy() if value is None else value
    selected_contract = contract() if contract_value is None else contract_value
    selected_after = [EXISTING_VM, CREATED_VM] if after is None else after
    with live_policy.ProvisioningLock() as host_lock:
        selected_permit = (live_policy.compile_provision_permit(
            selected_policy, selected_contract, run_id, [EXISTING_VM], trusted_digest(selected_policy), host_lock, now)
            if permit is None else permit)
        live_policy.claim_provision_permit_once(selected_permit, selected_policy, selected_contract,
                                                trusted_digest(selected_policy), host_lock, now)
        provisioner_uuid = live_policy.dispatch_provision_once(
            selected_permit, selected_policy, selected_contract, trusted_digest(selected_policy),
            selected_permit["createRequest"], host_lock, lambda _request: returned, now)
        return live_policy.bind_created_vm(selected_policy, selected_permit, selected_contract, selected_after,
                                           provisioner_uuid, trusted_digest(selected_policy), host_lock, now)


def test_policy_compiles_stable_one_vm_provision_permit_and_sealed_grant():
    contract_value = contract()
    live_policy.validate_live_policy(policy(), contract_value, trusted_digest(), NOW)
    first = permit_for()
    second = permit_for()
    assert first == second
    assert first["maxNewVmCount"] == 1
    assert first["inventoryBeforeUuids"] == [EXISTING_VM]
    assert first["createRequest"]["vmName"] == "live-ui-111111112222"

    grant = grant_for(contract_value=contract_value)
    checked = live_policy.validate_run_grant(grant, policy(), contract_value, trusted_digest(), NOW)
    assert checked["vmUuid"] == CREATED_VM
    assert checked["authorization"]["targetVmUuids"] == [CREATED_VM]
    assert checked["authorization"]["allowVmLifecycle"] is True
    assert checked["teardown"] == {"vmUuid": CREATED_VM, "on": ["success", "failure"], "maxAttempts": 1}
    assert checked["authorization"]["teardownOnNativePplFailure"] is False


def test_policy_canonicalizes_valid_uppercase_model_digest():
    value = policy()
    value["authorizationTemplate"]["ollamaModel"]["digest"] = MODEL_DIGEST.upper()
    trusted = trusted_digest(value)
    grant = grant_for(value=value)
    checked = live_policy.validate_run_grant(grant, value, contract(), trusted, NOW)
    assert checked["authorization"]["ollamaModel"]["digest"] == MODEL_DIGEST


def test_provision_permit_claim_is_private_and_non_replayable(tmp_path):
    with live_policy.ProvisioningLock() as host_lock:
        permit = live_policy.compile_provision_permit(policy(), contract(), RUN_ID, [EXISTING_VM],
                                                      trusted_digest(), host_lock, NOW)
        with pytest.raises(live_policy.LivePolicyError, match="claim is missing or unsafe"):
            live_policy.bind_created_vm(policy(), permit, contract(), [EXISTING_VM, CREATED_VM], CREATED_VM,
                                        trusted_digest(), host_lock, NOW)
        marker = live_policy.claim_provision_permit_once(permit, policy(), contract(), trusted_digest(), host_lock, NOW)
        with pytest.raises(live_policy.LivePolicyError, match="was not durably dispatched"):
            live_policy.bind_created_vm(policy(), permit, contract(), [EXISTING_VM, CREATED_VM], CREATED_VM,
                                        trusted_digest(), host_lock, NOW)
        provisioner_calls = []
        provisioner = lambda request: provisioner_calls.append(request) or CREATED_VM
        assert live_policy.dispatch_provision_once(permit, policy(), contract(), trusted_digest(),
                                                   permit["createRequest"], host_lock, provisioner, NOW) == CREATED_VM
        assert provisioner_calls == [permit["createRequest"]]
        with pytest.raises(live_policy.LivePolicyError, match="already been dispatched"):
            live_policy.dispatch_provision_once(permit, policy(), contract(), trusted_digest(),
                                                permit["createRequest"], host_lock, provisioner, NOW)
        assert len(provisioner_calls) == 1
        assert marker.stat().st_mode & 0o777 == 0o600
        assert json.loads(marker.read_text()) == {"permitSha256": permit["permitSha256"], "runId": RUN_ID}
        with pytest.raises(live_policy.LivePolicyError, match="already been claimed"):
            live_policy.claim_provision_permit_once(permit, policy(), contract(), trusted_digest(), host_lock, NOW)
        grant = live_policy.bind_created_vm(policy(), permit, contract(), [EXISTING_VM, CREATED_VM], CREATED_VM,
                                            trusted_digest(), host_lock, NOW)
    assert live_policy.validate_run_grant(grant, policy(), contract(), trusted_digest(), NOW)["vmUuid"] == CREATED_VM
    with live_policy.ProvisioningLock() as next_run:
        with pytest.raises(live_policy.LivePolicyError, match="already been claimed"):
            live_policy.claim_provision_permit_once(permit, policy(), contract(), trusted_digest(), next_run, NOW)


def test_provision_dispatch_refuses_changed_arguments_and_expired_permits(tmp_path):
    calls = []
    provisioner = lambda request: calls.append(request) or CREATED_VM
    with live_policy.ProvisioningLock() as host_lock:
        permit = live_policy.compile_provision_permit(policy(), contract(), RUN_ID, [EXISTING_VM],
                                                      trusted_digest(), host_lock, NOW)
        live_policy.claim_provision_permit_once(permit, policy(), contract(), trusted_digest(), host_lock, NOW)
        changed = dict(permit["createRequest"], vcpus=64)
        with pytest.raises(live_policy.LivePolicyError, match="differs from the one-use permit"):
            live_policy.dispatch_provision_once(permit, policy(), contract(), trusted_digest(), changed,
                                                host_lock, provisioner, NOW)
        with pytest.raises(live_policy.LivePolicyError, match="expired"):
            live_policy.dispatch_provision_once(permit, policy(), contract(), trusted_digest(),
                                                permit["createRequest"], host_lock,
                                                provisioner, NOW + timedelta(seconds=121))
        assert calls == []


def test_lost_provisioner_response_burns_dispatch_without_retry():
    calls = []
    def uncertain_provisioner(request):
        calls.append(request)
        raise TimeoutError("fixture lost response after possible create")

    with live_policy.ProvisioningLock() as host_lock:
        permit = live_policy.compile_provision_permit(policy(), contract(), RUN_ID, [EXISTING_VM],
                                                      trusted_digest(), host_lock, NOW)
        live_policy.claim_provision_permit_once(permit, policy(), contract(), trusted_digest(), host_lock, NOW)
        with pytest.raises(TimeoutError):
            live_policy.dispatch_provision_once(permit, policy(), contract(), trusted_digest(),
                                                permit["createRequest"], host_lock, uncertain_provisioner, NOW)
        with pytest.raises(live_policy.LivePolicyError, match="already been dispatched"):
            live_policy.dispatch_provision_once(permit, policy(), contract(), trusted_digest(),
                                                permit["createRequest"], host_lock, uncertain_provisioner, NOW)
    assert len(calls) == 1


def test_dispatch_expiry_after_durable_marker_prevents_provisioner_call(monkeypatch):
    permit = permit_for()
    original_utc = live_policy._utc
    observed_times = iter((NOW + timedelta(seconds=119), NOW + timedelta(seconds=119),
                           NOW + timedelta(seconds=121), NOW + timedelta(seconds=121)))
    monkeypatch.setattr(live_policy, "_utc", lambda value=None: next(observed_times) if value is None else original_utc(value))
    calls = []
    provisioner = lambda request: calls.append(request) or CREATED_VM
    with live_policy.ProvisioningLock() as host_lock:
        live_policy.claim_provision_permit_once(permit, policy(), contract(), trusted_digest(), host_lock, NOW)
        with pytest.raises(live_policy.LivePolicyError, match="expired"):
            live_policy.dispatch_provision_once(permit, policy(), contract(), trusted_digest(),
                                                permit["createRequest"], host_lock, provisioner)
    assert calls == []
    assert (live_policy._live_run_ledger_directory() / f"{RUN_ID}.create").exists()


def test_host_provisioning_lock_serializes_inventory_and_creation(tmp_path):
    with pytest.raises(live_policy.LivePolicyError, match="lock must be held"):
        live_policy.compile_provision_permit(policy(), contract(), RUN_ID, [EXISTING_VM], trusted_digest(), None, NOW)
    with live_policy.ProvisioningLock() as first:
        assert first._descriptor >= 0
        with pytest.raises(live_policy.LivePolicyError, match="another live run"):
            live_policy.ProvisioningLock()
    with live_policy.ProvisioningLock() as next_run:
        assert next_run._descriptor >= 0


@pytest.mark.parametrize("after,returned", [
    ([EXISTING_VM], CREATED_VM),
    ([EXISTING_VM, CREATED_VM, "00000000-0000-4000-8000-000000000003"], CREATED_VM),
    ([EXISTING_VM, CREATED_VM], "00000000-0000-4000-8000-000000000003"),
    ([CREATED_VM], CREATED_VM),
])
def test_uuid_binding_refuses_ambiguous_or_mismatched_host_inventory(after, returned):
    with pytest.raises(live_policy.LivePolicyError, match="inventory|exactly one newly created"):
        grant_for(after=after, returned=returned)


def test_permit_and_grant_reject_tampering_expiry_and_policy_substitution(tmp_path, monkeypatch):
    permit = permit_for()
    altered_permit = copy.deepcopy(permit)
    altered_permit["createRequest"]["vcpus"] = 64
    altered_permit["createRequestSha256"] = live_policy.sha256(altered_permit["createRequest"])
    altered_permit["permitSha256"] = live_policy.sha256({key: value for key, value in altered_permit.items() if key != "permitSha256"})
    with pytest.raises(live_policy.LivePolicyError, match="not derived from the trusted policy"):
        live_policy.validate_provision_permit(altered_permit, policy(), contract(), trusted_digest(), NOW)
    with pytest.raises(live_policy.LivePolicyError, match="expired"):
        live_policy.validate_provision_permit(permit, policy(), contract(), trusted_digest(), NOW + timedelta(seconds=121))
    monkeypatch.setattr(live_policy, "host_identity_sha256", lambda: "c" * 64)
    with pytest.raises(live_policy.LivePolicyError, match="trusted policy"):
        live_policy.validate_provision_permit(permit, policy(), contract(), trusted_digest(), NOW)
    monkeypatch.setattr(live_policy, "host_identity_sha256", lambda: HOST_IDENTITY)

    changed = policy()
    changed["provisioning"]["vcpus"] = 4
    with pytest.raises(live_policy.LivePolicyError, match="does not match the digest pinned"):
        live_policy.validate_live_policy(changed, contract(), trusted_digest(), NOW)
    wrong_host = policy()
    wrong_host["provisioning"]["hostIdentitySha256"] = "c" * 64
    with pytest.raises(live_policy.LivePolicyError, match="different host identity"):
        with live_policy.ProvisioningLock() as host_lock:
            live_policy.compile_provision_permit(wrong_host, contract(), RUN_ID, [EXISTING_VM],
                                                 trusted_digest(wrong_host), host_lock, NOW)
    secret_field = policy()
    secret_field["provisioning"]["apiKey"] = "MUST_NOT_BE_RETAINED"
    with pytest.raises(live_policy.LivePolicyError, match="secret-bearing fields"):
        live_policy.validate_live_policy(secret_field, contract(), trusted_digest(secret_field), NOW)
    grant = grant_for()
    altered_grant = copy.deepcopy(grant)
    altered_grant["authorization"]["approvedCalls"].append({"tool": "post_coordination_note", "arguments": {"message": "model-added"}})
    altered_grant["grantSha256"] = live_policy.sha256({key: value for key, value in altered_grant.items() if key != "grantSha256"})
    with pytest.raises(live_policy.LivePolicyError, match="authorization differs from the trusted policy"):
        live_policy.validate_run_grant(altered_grant, policy(), contract(), trusted_digest(), NOW)


def test_permit_claim_rechecks_expiry_after_durable_marker(tmp_path, monkeypatch):
    permit = permit_for()
    original_utc = live_policy._utc
    observed_times = iter((NOW + timedelta(seconds=119), NOW + timedelta(seconds=119),
                           NOW + timedelta(seconds=119), NOW + timedelta(seconds=119),
                           NOW + timedelta(seconds=121), NOW + timedelta(seconds=121)))

    def advancing_clock(value=None):
        return next(observed_times) if value is None else original_utc(value)

    monkeypatch.setattr(live_policy, "_utc", advancing_clock)
    with live_policy.ProvisioningLock() as host_lock:
        with pytest.raises(live_policy.LivePolicyError, match="expired"):
            live_policy.claim_provision_permit_once(permit, policy(), contract(), trusted_digest(), host_lock)
    marker = live_policy._live_run_ledger_directory() / f"{RUN_ID}.permit"
    assert marker.exists()


def test_broker_enforces_exact_call_order_and_teardown_once(tmp_path):
    contract_value = contract()
    grant = grant_for(contract_value=contract_value)
    broker = live_policy.LiveRunBroker(grant, policy(), contract_value, trusted_digest(),
                                       clock=lambda: NOW + timedelta(seconds=1))
    broker.grant["vmUuid"] = EXISTING_VM
    broker.authorization["approvedCalls"].append({"tool": "post_coordination_note", "arguments": {"message": "model-added"}})
    broker.calls.clear()
    first = broker.begin_call("get_host_capabilities", {}, CREATED_VM)
    assert first["callIndex"] == 0 and first["effect"] == "read-only"
    assert first["executesMcpResultCommands"] is False
    broker.complete_call(True)
    second = broker.begin_call("get_benchmark_series", {}, CREATED_VM)
    assert second["callIndex"] == 1
    broker.complete_call(True)
    assert broker.teardown_target() == {"vmUuid": CREATED_VM, "maxAttempts": 1, "grantSha256": grant["grantSha256"]}
    with pytest.raises(live_policy.LivePolicyError, match="already issued"):
        broker.teardown_target()


def test_broker_rejects_mcp_completion_after_grant_expiry():
    grant = grant_for()
    current_time = [NOW + timedelta(seconds=1)]
    broker = live_policy.LiveRunBroker(grant, policy(), contract(), trusted_digest(),
                                       clock=lambda: current_time[0])
    broker.begin_call("get_host_capabilities", {}, CREATED_VM)
    current_time[0] = NOW + timedelta(seconds=121)
    with pytest.raises(live_policy.LivePolicyError, match="expired while the MCP call was in flight"):
        broker.complete_call(True)
    assert broker.teardown_target()["vmUuid"] == CREATED_VM


def test_abort_with_inflight_call_blocks_teardown_until_call_settles():
    broker = live_policy.LiveRunBroker(grant_for(), policy(), contract(), trusted_digest(),
                                       clock=lambda: NOW + timedelta(seconds=1))
    broker.begin_call("get_host_capabilities", {}, CREATED_VM)
    broker.abort()
    with pytest.raises(live_policy.LivePolicyError, match="only after run termination"):
        broker.teardown_target()
    broker.complete_call(False)
    assert broker.teardown_target()["vmUuid"] == CREATED_VM


def test_broker_detects_host_identity_change_during_inflight_call(monkeypatch):
    grant = grant_for()
    broker = live_policy.LiveRunBroker(grant, policy(), contract(), trusted_digest(),
                                       clock=lambda: NOW + timedelta(seconds=1))
    broker.begin_call("get_host_capabilities", {}, CREATED_VM)
    monkeypatch.setattr(live_policy, "host_identity_sha256", lambda: "c" * 64)
    with pytest.raises(live_policy.LivePolicyError, match="current host identity differs"):
        broker.complete_call(True)
    assert broker.grant["vmUuid"] == CREATED_VM
    with pytest.raises(live_policy.LivePolicyError, match="host identity differs from the sealed teardown target"):
        broker.teardown_target()


def test_broker_denies_changed_arguments_and_wrong_vm_then_only_tears_down_bound_vm(tmp_path):
    broker = live_policy.LiveRunBroker(grant_for(), policy(), contract(), trusted_digest(),
                                       clock=lambda: NOW + timedelta(seconds=1))
    with pytest.raises(live_policy.LivePolicyError, match="differs from the next exact"):
        broker.begin_call("get_host_capabilities", {"extra": True}, CREATED_VM)
    assert broker.teardown_target()["vmUuid"] == CREATED_VM

    wrong_vm = live_policy.LiveRunBroker(grant_for(run_id="22222222-3333-4444-8555-666666666666"),
                                         policy(), contract(), trusted_digest(),
                                         clock=lambda: NOW + timedelta(seconds=1))
    with pytest.raises(live_policy.LivePolicyError, match="identity or time bound"):
        wrong_vm.begin_call("get_host_capabilities", {}, EXISTING_VM)
    assert wrong_vm.teardown_target()["vmUuid"] == CREATED_VM


def test_plan_only_tool_result_is_marked_inert_and_failure_does_not_retry(tmp_path):
    contract_value = contract()
    value = policy()
    value["authorizationTemplate"]["approvedCalls"] = [{"tool": "spin_up_witness", "arguments": {"plane": "VBOX"}}]
    grant = grant_for(value=value, contract_value=contract_value)
    broker = live_policy.LiveRunBroker(grant, value, contract_value, trusted_digest(value),
                                       clock=lambda: NOW + timedelta(seconds=1))
    decision = broker.begin_call("spin_up_witness", {"plane": "VBOX"}, CREATED_VM)
    assert decision["effect"] == "plan-only"
    assert decision["executesMcpResultCommands"] is False
    broker.complete_call(False)
    with pytest.raises(live_policy.LivePolicyError, match="terminal or has an in-flight"):
        broker.begin_call("spin_up_witness", {"plane": "VBOX"}, CREATED_VM)
    assert broker.teardown_target()["vmUuid"] == CREATED_VM


def test_sealed_run_grant_cannot_be_replayed_in_a_second_broker(tmp_path):
    value = policy()
    contract_value = contract()
    grant = grant_for(value=value, contract_value=contract_value)
    live_policy.LiveRunBroker(grant, value, contract_value, trusted_digest(value),
                              clock=lambda: NOW + timedelta(seconds=1))
    with pytest.raises(live_policy.LivePolicyError, match="already been claimed"):
        live_policy.LiveRunBroker(grant, value, contract_value, trusted_digest(value),
                                  clock=lambda: NOW + timedelta(seconds=1))


def test_multiserver_grant_binds_exact_server_identity_through_dispatch():
    from types import SimpleNamespace

    from assisted_benchmark_mcp_router import McpRouterError, PinnedMcpRouter

    with pytest.raises(McpRouterError, match="requires a pinned MCP server set"):
        PinnedMcpRouter(contract(), {}, {}, {})

    contract_value = server_set_contract()
    value = policy()
    value["sourceRevision"] = contract_value["sourceRevision"]
    value["provisioning"]["guestOs"] = "windows"
    value["authorizationTemplate"]["approvedCalls"] = [
        {"serverId": "lba", "tool": "get_host_capabilities", "arguments": {}},
    ]
    value["authorizationTemplate"]["approvedCallChoices"] = [{
        "choiceId": "decision",
        "selection": "exactly-one",
        "candidates": [
            {"serverId": "synthetic-ui", "tool": "click_target",
             "arguments": {"taskId": "synthetic-00000063", "targetId": "approve"}},
            {"serverId": "synthetic-ui", "tool": "click_target",
             "arguments": {"taskId": "synthetic-00000063", "targetId": "hold"}},
        ],
    }]
    grant = grant_for(value=value, contract_value=contract_value)
    broker = live_policy.LiveRunBroker(grant, value, contract_value, trusted_digest(value),
                                       clock=lambda: NOW + timedelta(seconds=1))

    def snapshot(server_contract):
        source = server_contract["source"]
        tools = [tool for group in server_contract["toolGroups"].values() for tool in group["tools"]]
        return {
            "protocolVersion": source["protocolVersion"],
            "serverInfo": {"name": source["serverName"], "version": source["serverVersion"]},
            "tools": [{"name": tool["name"], "description": tool["name"],
                       "inputSchema": tool["inputSchema"]} for tool in tools],
        }

    class RecordingClient:
        def __init__(self, response=None):
            self.calls = []
            self.response = {"ok": True} if response is None else response

        async def call_tool(self, tool_name, arguments):
            self.calls.append((tool_name, arguments))
            return self.response

    clients = {server_id: RecordingClient() for server_id in contract_value["servers"]}
    snapshots = {
        server_id: snapshot(server_contract)
        for server_id, server_contract in contract_value["servers"].items()
    }
    router = PinnedMcpRouter(
        contract_value,
        grant["authorization"],
        snapshots,
        clients,
    )
    altered_snapshots = copy.deepcopy(snapshots)
    altered_snapshots["synthetic-ui"]["serverInfo"]["name"] = "different-server"
    with pytest.raises(McpRouterError, match="capability snapshot differs"):
        PinnedMcpRouter(contract_value, grant["authorization"], altered_snapshots, clients)
    with pytest.raises(McpRouterError, match="exactly match"):
        PinnedMcpRouter(contract_value, grant["authorization"], snapshots, {"lba": clients["lba"]})

    first = broker.begin_call("get_host_capabilities", {}, CREATED_VM, server_id="lba")
    assert first["serverId"] == "lba"
    missing_inertness = {key: item for key, item in first.items() if key != "executesMcpResultCommands"}
    with pytest.raises(McpRouterError, match="effect differs from the pinned server tool"):
        asyncio.run(router.dispatch(missing_inertness))
    assert clients["lba"].calls == []
    assert asyncio.run(router.dispatch(first)) == {"ok": True}
    broker.complete_call(True)
    second = broker.begin_call("click_target", {"taskId": "synthetic-00000063", "targetId": "approve"},
                               CREATED_VM, server_id="synthetic-ui")
    assert second["serverId"] == "synthetic-ui"
    assert second["choiceId"] == "decision"
    assert second["effect"] == "coordination-write"
    assert second["choiceId"] == "decision"
    assert asyncio.run(router.dispatch(second)) == {"ok": True}
    broker.complete_call(True)
    with pytest.raises(live_policy.LivePolicyError, match="terminal or has an in-flight"):
        broker.begin_call("click_target", {"taskId": "synthetic-00000063", "targetId": "hold"},
                          CREATED_VM, server_id="synthetic-ui")
    second_choice = {**second, "arguments": {"taskId": "synthetic-00000063", "targetId": "hold"}}
    with pytest.raises(McpRouterError, match="authorization sequence is exhausted"):
        asyncio.run(router.dispatch(second_choice))
    assert clients["lba"].calls == [("get_host_capabilities", {})]
    assert clients["synthetic-ui"].calls == [
        ("click_target", {"taskId": "synthetic-00000063", "targetId": "approve"})
    ]
    altered_decision = {**second, "serverIdentity": contract_value["servers"]["lba"]["source"]}
    with pytest.raises(McpRouterError, match="identity or contract digest changed"):
        asyncio.run(router.dispatch(altered_decision))

    action_policy = policy()
    action_policy["sourceRevision"] = contract_value["sourceRevision"]
    action_policy["policyId"] = "synthetic-ui-route-tamper"
    action_policy["provisioning"]["guestOs"] = "windows"
    action_policy["authorizationTemplate"]["approvedCalls"] = [
        {"serverId": "lba", "tool": "get_host_capabilities", "arguments": {}},
    ]
    action_policy["authorizationTemplate"]["approvedCallChoices"] = [{
        "choiceId": "decision", "selection": "exactly-one",
        "candidates": [
            {"serverId": "synthetic-ui", "tool": "click_target",
             "arguments": {"taskId": "synthetic-00000063", "targetId": "approve"}},
            {"serverId": "synthetic-ui", "tool": "click_target",
             "arguments": {"taskId": "synthetic-00000063", "targetId": "hold"}},
        ],
    }]
    action_grant = grant_for(value=action_policy, contract_value=contract_value,
                             run_id="33333333-4444-4555-8666-777777777777")
    action_broker = live_policy.LiveRunBroker(
        action_grant, action_policy, contract_value, trusted_digest(action_policy),
        clock=lambda: NOW + timedelta(seconds=1),
    )
    action_clients = {server_id: RecordingClient() for server_id in contract_value["servers"]}
    action_router = PinnedMcpRouter(contract_value, action_grant["authorization"], snapshots, action_clients)
    action_preamble = action_broker.begin_call("get_host_capabilities", {}, CREATED_VM, server_id="lba")
    asyncio.run(action_router.dispatch(action_preamble))
    action_broker.complete_call(True)
    decision = action_broker.begin_call(
        "click_target", {"taskId": "synthetic-00000063", "targetId": "approve"},
        CREATED_VM, server_id="synthetic-ui",
    )
    altered_effect = {**decision, "effect": "read-only"}
    with pytest.raises(McpRouterError, match="effect differs from the pinned server tool"):
        asyncio.run(action_router.dispatch(altered_effect))
    assert action_clients["synthetic-ui"].calls == []
    decision["arguments"]["targetId"] = "unapproved"
    with pytest.raises(McpRouterError, match="differs from the next exact grant call"):
        asyncio.run(action_router.dispatch(decision))
    assert action_clients["synthetic-ui"].calls == []
    action_broker.complete_call(False)
    with pytest.raises(live_policy.LivePolicyError, match="terminal or has an in-flight"):
        action_broker.begin_call("click_target", {"taskId": "synthetic-00000063", "targetId": "hold"},
                                 CREATED_VM, server_id="synthetic-ui")
    assert action_broker.teardown_target()["vmUuid"] == CREATED_VM

    failed_policy = choice_policy(contract_value)
    failed_value = failed_policy[0]
    failed_value["policyId"] = "synthetic-ui-choice-failure"
    failed_value["provisioning"]["guestOs"] = "windows"
    failed_grant = grant_for(value=failed_value, contract_value=contract_value,
                             run_id="77777777-8888-4999-8aaa-bbbbbbbbbbbb")
    failed_broker = live_policy.LiveRunBroker(
        failed_grant, failed_value, contract_value, trusted_digest(failed_value),
        clock=lambda: NOW + timedelta(seconds=1),
    )
    failed_clients = {
        "lba": RecordingClient(),
        "synthetic-ui": RecordingClient(SimpleNamespace(is_error=True)),
    }
    failed_router = PinnedMcpRouter(
        contract_value, failed_grant["authorization"], snapshots, failed_clients,
    )
    failed_preamble = failed_broker.begin_call("get_host_capabilities", {}, CREATED_VM, server_id="lba")
    asyncio.run(failed_router.dispatch(failed_preamble))
    failed_broker.complete_call(True)
    failed_decision = failed_broker.begin_call(
        "click_target", {"taskId": "synthetic-00000063", "targetId": "approve"},
        CREATED_VM, server_id="synthetic-ui",
    )
    with pytest.raises(McpRouterError, match="client rejected"):
        asyncio.run(failed_router.dispatch(failed_decision))
    failed_broker.complete_call(False)
    with pytest.raises(McpRouterError, match="authorization sequence is exhausted"):
        asyncio.run(failed_router.dispatch({
            **failed_decision,
            "arguments": {"taskId": "synthetic-00000063", "targetId": "hold"},
        }))
    assert failed_clients["synthetic-ui"].calls == [
        ("click_target", {"taskId": "synthetic-00000063", "targetId": "approve"})
    ]
    assert failed_broker.teardown_target()["vmUuid"] == CREATED_VM

    expiry_policy = policy()
    expiry_policy["sourceRevision"] = contract_value["sourceRevision"]
    expiry_policy["policyId"] = "synthetic-ui-dispatch-expiry"
    expiry_policy["provisioning"]["guestOs"] = "windows"
    expiry_policy["authorizationTemplate"]["approvedCalls"] = [
        {"serverId": "lba", "tool": "get_host_capabilities", "arguments": {}},
    ]
    expiry_policy["authorizationTemplate"]["approvedCallChoices"] = [{
        "choiceId": "decision", "selection": "exactly-one",
        "candidates": [
            {"serverId": "synthetic-ui", "tool": "click_target",
             "arguments": {"taskId": "synthetic-00000063", "targetId": "approve"}},
            {"serverId": "synthetic-ui", "tool": "click_target",
             "arguments": {"taskId": "synthetic-00000063", "targetId": "hold"}},
        ],
    }]
    expiry_grant = grant_for(value=expiry_policy, contract_value=contract_value,
                             run_id="66666666-7777-4888-8999-aaaaaaaaaaaa")
    expiry_broker = live_policy.LiveRunBroker(
        expiry_grant, expiry_policy, contract_value, trusted_digest(expiry_policy),
        clock=lambda: NOW + timedelta(seconds=1),
    )
    expiry_clients = {server_id: RecordingClient() for server_id in contract_value["servers"]}
    expiry_time = [NOW + timedelta(seconds=1)]
    expiry_router = PinnedMcpRouter(
        contract_value, expiry_grant["authorization"], snapshots, expiry_clients,
        clock=lambda: expiry_time[0],
    )
    expiry_preamble = expiry_broker.begin_call("get_host_capabilities", {}, CREATED_VM, server_id="lba")
    asyncio.run(expiry_router.dispatch(expiry_preamble))
    expiry_broker.complete_call(True)
    expiry_time[0] = NOW + timedelta(seconds=121)
    expiry_decision = expiry_broker.begin_call(
        "click_target", {"taskId": "synthetic-00000063", "targetId": "approve"},
        CREATED_VM, server_id="synthetic-ui",
    )
    with pytest.raises(McpRouterError, match="expired before client dispatch"):
        asyncio.run(expiry_router.dispatch(expiry_decision))
    assert expiry_clients["synthetic-ui"].calls == []
    expiry_broker.complete_call(False)
    assert expiry_broker.teardown_target()["vmUuid"] == CREATED_VM

    wrong_policy = {**value, "policyId": "synthetic-live-ui-wrong-route"}
    wrong_route = live_policy.LiveRunBroker(
        grant_for(value=wrong_policy, contract_value=contract_value,
                  run_id="22222222-3333-4444-8555-666666666666"),
        wrong_policy, contract_value, trusted_digest(wrong_policy),
        clock=lambda: NOW + timedelta(seconds=1),
    )
    wrong_route.begin_call("get_host_capabilities", {}, CREATED_VM, server_id="lba")
    wrong_route.complete_call(True)
    with pytest.raises(live_policy.LivePolicyError, match="differs from the next exact"):
        wrong_route.begin_call("click_target", {"taskId": "synthetic-00000063", "targetId": "approve"},
                               CREATED_VM, server_id="lba")
    assert wrong_route.teardown_target()["vmUuid"] == CREATED_VM


def test_stdio_ui_action_flows_through_server_set_broker_router_and_oracle():
    import socket
    import subprocess
    from urllib.request import urlopen

    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    from assisted_benchmark_mcp_router import PinnedMcpRouter
    from assisted_benchmark_synthetic_oracle import SyntheticUiOracle

    async def exercise_run():
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
                tools = await session.list_tools()
                contract_value = server_set_contract(initialization.protocol_version)
                value = policy()
                value["sourceRevision"] = contract_value["sourceRevision"]
                value["provisioning"]["guestOs"] = "windows"
                value["authorizationTemplate"]["approvedCalls"] = [
                    {"serverId": "lba", "tool": "get_host_capabilities", "arguments": {}},
                ]
                value["authorizationTemplate"]["approvedCallChoices"] = [{
                    "choiceId": "decision", "selection": "exactly-one",
                    "candidates": [
                        {"serverId": "synthetic-ui", "tool": "click_target",
                         "arguments": {"taskId": "synthetic-00000064", "targetId": "approve"}},
                        {"serverId": "synthetic-ui", "tool": "click_target",
                         "arguments": {"taskId": "synthetic-00000064", "targetId": "hold"}},
                    ],
                }]
                grant = grant_for(value=value, contract_value=contract_value,
                                  run_id="55555555-6666-4777-8888-999999999999")
                broker = live_policy.LiveRunBroker(
                    grant, value, contract_value, trusted_digest(value),
                    clock=lambda: NOW + timedelta(seconds=1),
                )

                class LbaFixtureClient:
                    async def call_tool(self, _tool_name, _arguments):
                        return {"fixture": "lba-read"}

                server_snapshots = {}
                for server_id, server_contract in contract_value["servers"].items():
                    source = server_contract["source"]
                    server_snapshots[server_id] = {
                        "protocolVersion": source["protocolVersion"],
                        "serverInfo": {"name": source["serverName"], "version": source["serverVersion"]},
                        "tools": [
                            {"name": tool["name"], "description": tool["name"], "inputSchema": tool["inputSchema"]}
                            for group in server_contract["toolGroups"].values() for tool in group["tools"]
                        ],
                    }
                server_snapshots["synthetic-ui"] = {
                    "protocolVersion": initialization.protocol_version,
                    "serverInfo": {"name": initialization.server_info.name, "version": initialization.server_info.version},
                    "tools": [{"name": tool.name, "description": tool.description,
                               "inputSchema": tool.input_schema} for tool in tools.tools],
                }
                router = PinnedMcpRouter(
                    contract_value, grant["authorization"], server_snapshots,
                    {"lba": LbaFixtureClient(), "synthetic-ui": session},
                )

                def read_view():
                    with urlopen(f"http://127.0.0.1:{ui_port}/api/view") as response:
                        return json.loads(response.read())

                before = read_view()
                lba_decision = broker.begin_call("get_host_capabilities", {}, CREATED_VM, server_id="lba")
                assert await router.dispatch(lba_decision) == {"fixture": "lba-read"}
                broker.complete_call(True)
                model_selected_target = "hold"
                decision = broker.begin_call(
                    "click_target", {"taskId": before["taskId"], "targetId": model_selected_target},
                    CREATED_VM, server_id="synthetic-ui",
                )
                assert decision["choiceId"] == "decision"
                result = await router.dispatch(decision)
                broker.complete_call(result.is_error is not True)
                after = read_view()
                oracle = SyntheticUiOracle().evaluate_observation(
                    after["taskId"], after,
                    decision["arguments"]["targetId"],
                    {"reading": before["reading"], "limit": before["limit"]},
                )
                return broker, result, before, after, oracle

    broker, result, before, after, oracle = asyncio.run(exercise_run())

    assert result.is_error is not True
    assert before["state"] == "awaiting_decision"
    assert after["state"] == "decision_recorded"
    assert oracle.state_grounded is True
    assert oracle.action_correct is True
    assert oracle.task_success is True
    assert broker.teardown_target()["vmUuid"] == CREATED_VM


def choice_policy(contract_value=None):
    selected_contract = server_set_contract() if contract_value is None else contract_value
    value = policy()
    value["sourceRevision"] = selected_contract["sourceRevision"]
    value["authorizationTemplate"]["approvedCalls"] = [
        {"serverId": "lba", "tool": "get_host_capabilities", "arguments": {}},
    ]
    value["authorizationTemplate"]["approvedCallChoices"] = [{
        "choiceId": "decision",
        "selection": "exactly-one",
        "candidates": [
            {"serverId": "synthetic-ui", "tool": "click_target",
             "arguments": {"taskId": "synthetic-00000063", "targetId": "approve"}},
            {"serverId": "synthetic-ui", "tool": "click_target",
             "arguments": {"taskId": "synthetic-00000063", "targetId": "hold"}},
        ],
    }]
    return value, selected_contract


@pytest.mark.parametrize("problem", ["candidate-count", "duplicate", "server", "tool", "different-tool"])
def test_call_choice_group_is_narrow_and_server_bound(problem):
    value, contract_value = choice_policy()
    candidates = value["authorizationTemplate"]["approvedCallChoices"][0]["candidates"]
    if problem == "candidate-count":
        candidates.pop()
    elif problem == "duplicate":
        candidates[1] = copy.deepcopy(candidates[0])
    elif problem == "server":
        candidates[1]["serverId"] = "other-server"
    elif problem == "different-tool":
        ui_tools = contract_value["servers"]["synthetic-ui"]["toolGroups"]["core"]["tools"]
        other_tool = copy.deepcopy(ui_tools[0])
        other_tool["name"] = "alternate_click"
        other_tool["inputSchema"] = {
            "type": "object", "properties": {}, "additionalProperties": False,
        }
        ui_tools.append(other_tool)
        candidates[1]["tool"] = "alternate_click"
        candidates[1]["arguments"] = {}
    else:
        candidates[1]["tool"] = "get_host_capabilities"

    with pytest.raises(live_policy.LivePolicyError, match="authorization template is invalid"):
        live_policy.validate_live_policy(value, contract_value, trusted_digest(value), NOW)


def test_call_choice_groups_are_rejected_for_legacy_single_server_contract():
    value = policy()
    value["authorizationTemplate"]["approvedCallChoices"] = [{
        "choiceId": "decision",
        "selection": "exactly-one",
        "candidates": [
            {"serverId": "lba", "tool": "get_host_capabilities", "arguments": {}},
            {"serverId": "lba", "tool": "get_benchmark_series", "arguments": {}},
        ],
    }]

    with pytest.raises(live_policy.LivePolicyError, match="call choice groups require a pinned MCP server set"):
        live_policy.validate_live_policy(value, contract(), trusted_digest(value), NOW)


def test_call_choice_candidates_remain_bound_to_trusted_policy_after_grant_rehash():
    value, contract_value = choice_policy()
    grant = grant_for(value=value, contract_value=contract_value,
                      run_id="88888888-9999-4aaa-8bbb-cccccccccccc")
    altered_grant = copy.deepcopy(grant)
    altered_grant["authorization"]["approvedCallChoices"][0]["candidates"][0]["arguments"]["taskId"] = "synthetic-00000064"
    altered_core = {key: item for key, item in altered_grant.items() if key != "grantSha256"}
    altered_grant["grantSha256"] = live_policy.sha256(altered_core)

    with pytest.raises(live_policy.LivePolicyError, match="authorization differs from the trusted policy"):
        live_policy.validate_run_grant(altered_grant, value, contract_value, trusted_digest(value), NOW)