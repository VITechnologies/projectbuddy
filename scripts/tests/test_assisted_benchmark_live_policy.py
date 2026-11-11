from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY / "scripts"))

import assisted_benchmark_live_policy as live_policy
derive_host_identity_sha256 = live_policy.host_identity_sha256

CONTRACT_PATH = REPOSITORY / "tools" / "assisted-benchmark" / "lba-mcp-contract.json"
RUN_ID = "11111111-2222-4333-8444-555555555555"
EXISTING_VM = "00000000-0000-4000-8000-000000000001"
CREATED_VM = "00000000-0000-4000-8000-000000000002"
NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
MODEL_DIGEST = "845dbda0ea48ed749caafd9e6037047aa19acfcfd82e704d7ca97d631a0b697e"
HOST_IDENTITY = "b" * 64


@pytest.fixture(autouse=True)
def fake_host_identity(monkeypatch):
    monkeypatch.setattr(live_policy, "host_identity_sha256", lambda: HOST_IDENTITY)


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


def contract():
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


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


def permit_for(value=None, contract_value=None, inventory=None, now=NOW):
    selected_policy = policy() if value is None else value
    selected_contract = contract() if contract_value is None else contract_value
    with tempfile.TemporaryDirectory() as directory:
        ledger = Path(directory)
        ledger.chmod(0o700)
        with live_policy.ProvisioningLock(ledger) as host_lock:
            return live_policy.compile_provision_permit(
                selected_policy, selected_contract, RUN_ID,
                [EXISTING_VM] if inventory is None else inventory,
                trusted_digest(selected_policy), host_lock, now)


def grant_for(value=None, contract_value=None, permit=None, after=None, returned=CREATED_VM, now=NOW):
    selected_policy = policy() if value is None else value
    selected_contract = contract() if contract_value is None else contract_value
    selected_after = [EXISTING_VM, CREATED_VM] if after is None else after
    with tempfile.TemporaryDirectory() as directory:
        ledger = Path(directory)
        ledger.chmod(0o700)
        with live_policy.ProvisioningLock(ledger) as host_lock:
            selected_permit = (live_policy.compile_provision_permit(
                selected_policy, selected_contract, RUN_ID, [EXISTING_VM], trusted_digest(selected_policy), host_lock, now)
                if permit is None else permit)
            claim = live_policy.claim_provision_permit_once(selected_permit, selected_policy, selected_contract,
                                                            trusted_digest(selected_policy), ledger, host_lock, now)
            return live_policy.bind_created_vm(selected_policy, selected_permit, selected_contract, selected_after,
                                               returned, trusted_digest(selected_policy), claim, host_lock, now)


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


def test_provision_permit_claim_is_private_and_non_replayable(tmp_path):
    ledger = tmp_path / "ledger"
    ledger.mkdir(mode=0o700)
    ledger.chmod(0o700)
    with live_policy.ProvisioningLock(ledger) as host_lock:
        permit = live_policy.compile_provision_permit(policy(), contract(), RUN_ID, [EXISTING_VM],
                                                      trusted_digest(), host_lock, NOW)
        marker = live_policy.claim_provision_permit_once(permit, policy(), contract(), trusted_digest(), ledger,
                                                         host_lock, NOW)
        assert marker.stat().st_mode & 0o777 == 0o600
        assert json.loads(marker.read_text()) == {"permitSha256": permit["permitSha256"], "runId": RUN_ID}
        with pytest.raises(live_policy.LivePolicyError, match="already been claimed"):
            live_policy.claim_provision_permit_once(permit, policy(), contract(), trusted_digest(), ledger,
                                                    host_lock, NOW)
        other_ledger = tmp_path / "other-ledger"
        other_ledger.mkdir(mode=0o700)
        with pytest.raises(live_policy.LivePolicyError, match="ledger differs from the locked host directory"):
            live_policy.claim_provision_permit_once(permit, policy(), contract(), trusted_digest(), other_ledger,
                                                    host_lock, NOW)
        with pytest.raises(live_policy.LivePolicyError, match="claim is missing or unsafe"):
            live_policy.bind_created_vm(policy(), permit, contract(), [EXISTING_VM, CREATED_VM], CREATED_VM,
                                        trusted_digest(), ledger / f"{RUN_ID}.missing", host_lock, NOW)
        with pytest.raises(live_policy.LivePolicyError, match="lock must be held"):
            live_policy.bind_created_vm(policy(), permit, contract(), [EXISTING_VM, CREATED_VM], CREATED_VM,
                                        trusted_digest(), marker, None, NOW)
        grant = live_policy.bind_created_vm(policy(), permit, contract(), [EXISTING_VM, CREATED_VM], CREATED_VM,
                                            trusted_digest(), marker, host_lock, NOW)
    assert live_policy.validate_run_grant(grant, policy(), contract(), trusted_digest(), NOW)["vmUuid"] == CREATED_VM


def test_host_provisioning_lock_serializes_inventory_and_creation(tmp_path):
    ledger = tmp_path / "ledger"
    ledger.mkdir(mode=0o700)
    ledger.chmod(0o700)
    with pytest.raises(live_policy.LivePolicyError, match="lock must be held"):
        live_policy.compile_provision_permit(policy(), contract(), RUN_ID, [EXISTING_VM], trusted_digest(), None, NOW)
    with live_policy.ProvisioningLock(ledger) as first:
        assert first._descriptor >= 0
        with pytest.raises(live_policy.LivePolicyError, match="another live run"):
            live_policy.ProvisioningLock(ledger)
    with live_policy.ProvisioningLock(ledger) as next_run:
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


def test_permit_and_grant_reject_tampering_expiry_and_policy_substitution(tmp_path):
    permit = permit_for()
    altered_permit = copy.deepcopy(permit)
    altered_permit["createRequest"]["vcpus"] = 64
    altered_permit["createRequestSha256"] = live_policy.sha256(altered_permit["createRequest"])
    altered_permit["permitSha256"] = live_policy.sha256({key: value for key, value in altered_permit.items() if key != "permitSha256"})
    with pytest.raises(live_policy.LivePolicyError, match="not derived from the trusted policy"):
        live_policy.validate_provision_permit(altered_permit, policy(), contract(), trusted_digest(), NOW)
    with pytest.raises(live_policy.LivePolicyError, match="expired"):
        live_policy.validate_provision_permit(permit, policy(), contract(), trusted_digest(), NOW + timedelta(seconds=121))

    changed = policy()
    changed["provisioning"]["vcpus"] = 4
    with pytest.raises(live_policy.LivePolicyError, match="does not match the digest pinned"):
        live_policy.validate_live_policy(changed, contract(), trusted_digest(), NOW)
    wrong_host = policy()
    wrong_host["provisioning"]["hostIdentitySha256"] = "c" * 64
    with pytest.raises(live_policy.LivePolicyError, match="different host identity"):
        ledger = tmp_path / "wrong-host"
        ledger.mkdir(mode=0o700)
        with live_policy.ProvisioningLock(ledger) as host_lock:
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


def test_broker_enforces_exact_call_order_and_teardown_once(tmp_path):
    contract_value = contract()
    grant = grant_for(contract_value=contract_value)
    ledger = tmp_path / "ledger"
    ledger.mkdir(mode=0o700)
    ledger.chmod(0o700)
    broker = live_policy.LiveRunBroker(grant, policy(), contract_value, trusted_digest(), ledger,
                                       clock=lambda: NOW + timedelta(seconds=1))
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


def test_broker_denies_changed_arguments_and_wrong_vm_then_only_tears_down_bound_vm(tmp_path):
    broker_ledger = tmp_path / "changed-call"
    broker_ledger.mkdir(mode=0o700)
    broker = live_policy.LiveRunBroker(grant_for(), policy(), contract(), trusted_digest(), broker_ledger,
                                       clock=lambda: NOW + timedelta(seconds=1))
    with pytest.raises(live_policy.LivePolicyError, match="differs from the next exact"):
        broker.begin_call("get_host_capabilities", {"extra": True}, CREATED_VM)
    assert broker.teardown_target()["vmUuid"] == CREATED_VM

    wrong_ledger = tmp_path / "wrong-vm"
    wrong_ledger.mkdir(mode=0o700)
    wrong_vm = live_policy.LiveRunBroker(grant_for(), policy(), contract(), trusted_digest(), wrong_ledger,
                                         clock=lambda: NOW + timedelta(seconds=1))
    with pytest.raises(live_policy.LivePolicyError, match="identity or time bound"):
        wrong_vm.begin_call("get_host_capabilities", {}, EXISTING_VM)
    assert wrong_vm.teardown_target()["vmUuid"] == CREATED_VM


def test_plan_only_tool_result_is_marked_inert_and_failure_does_not_retry(tmp_path):
    contract_value = contract()
    value = policy()
    value["authorizationTemplate"]["approvedCalls"] = [{"tool": "spin_up_witness", "arguments": {"plane": "VBOX"}}]
    grant = grant_for(value=value, contract_value=contract_value)
    ledger = tmp_path / "ledger"
    ledger.mkdir(mode=0o700)
    broker = live_policy.LiveRunBroker(grant, value, contract_value, trusted_digest(value), ledger,
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
    ledger = tmp_path / "ledger"
    ledger.mkdir(mode=0o700)
    live_policy.LiveRunBroker(grant, value, contract_value, trusted_digest(value), ledger,
                              clock=lambda: NOW + timedelta(seconds=1))
    with pytest.raises(live_policy.LivePolicyError, match="already been claimed"):
        live_policy.LiveRunBroker(grant, value, contract_value, trusted_digest(value), ledger,
                                  clock=lambda: NOW + timedelta(seconds=1))