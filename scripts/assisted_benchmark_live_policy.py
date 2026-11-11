"""Compile deterministic, VM-bound authority for live assisted runs."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from assisted_benchmark_contract import ContractError, authorize_tool_call, validate_authorization

POLICY_SCHEMA = "projectbuddy/assisted-live-run-policy@1"
PROVISION_SCHEMA = "projectbuddy/assisted-live-vm-permit@1"
GRANT_SCHEMA = "projectbuddy/assisted-live-run-grant@1"
MAX_POLICY_LIFETIME_SECONDS = 900
AUTHORIZATION_TEMPLATE_KEYS = {
    "workload", "approvedCalls", "evidenceFields", "ollamaModel", "reviewBudget",
    "reviewPrivacy", "reviewLifecycle", "reviewExecution",
}
PROVISIONING_KEYS = {
    "hostIdentitySha256", "hostProfile", "guestOs", "baseImageSha256", "vmNamePrefix", "vcpus",
    "memoryMb", "diskGb", "maxVms",
}
SENSITIVE_FIELD_MARKERS = {"password", "passwd", "secret", "credential", "apikey", "privatekey", "accesskey"}


class LivePolicyError(ValueError):
    """A deterministic live-run policy or grant failed closed."""


class ProvisioningLock:
    """Serialize host inventory and VM creation for a single live run."""

    def __init__(self, ledger_directory: Path):
        if os.name != "posix":
            raise LivePolicyError("host provisioning lock is unsupported on this platform")
        self.directory = Path(ledger_directory)
        if (not self.directory.is_absolute() or self.directory.is_symlink()
                or self.directory.resolve(strict=True) != self.directory):
            raise LivePolicyError("provisioning lock directory must be absolute and non-symlinked")
        state = self.directory.stat()
        if state.st_uid != os.getuid() or stat.S_IMODE(state.st_mode) != 0o700:
            raise LivePolicyError("provisioning lock directory requires private current-user custody")
        try:
            import fcntl
        except ImportError:
            raise LivePolicyError("host provisioning lock is unavailable") from None
        self._fcntl = fcntl
        self._descriptor = -1
        self.acquired = False
        path = self.directory / "host-provisioning.lock"
        try:
            self._descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            state = os.fstat(self._descriptor)
            if (not stat.S_ISREG(state.st_mode) or state.st_uid != os.getuid()
                    or stat.S_IMODE(state.st_mode) != 0o600 or state.st_nlink != 1):
                self.close()
                raise LivePolicyError("host provisioning lock custody is unsafe")
            self._fcntl.flock(self._descriptor, self._fcntl.LOCK_EX | self._fcntl.LOCK_NB)
            self.acquired = True
        except BlockingIOError:
            self.close()
            raise LivePolicyError("another live run holds the host provisioning lock") from None
        except OSError:
            self.close()
            raise LivePolicyError("host provisioning lock could not be acquired") from None

    def __enter__(self):
        return self

    def close(self):
        if self._descriptor >= 0:
            if self._fcntl is not None:
                try:
                    self._fcntl.flock(self._descriptor, self._fcntl.LOCK_UN)
                except OSError:
                    pass
            os.close(self._descriptor)
            self._descriptor = -1
            self.acquired = False

    def __exit__(self, *_error):
        self.close()


def canonical_json(value: object) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        raise LivePolicyError("policy data is not canonical JSON") from None


def sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def host_identity_sha256(path: Path = Path("/etc/machine-id")) -> str:
    if os.name != "posix" or not path.is_absolute():
        raise LivePolicyError("current host identity source is unsupported")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as handle:
            state = os.fstat(handle.fileno())
            raw = handle.read(65).strip().lower()
    except OSError:
        raise LivePolicyError("current host identity is unavailable") from None
    if not stat.S_ISREG(state.st_mode) or not re.fullmatch(rb"[a-f0-9]{32}", raw) or raw == b"0" * 32:
        raise LivePolicyError("current host identity is invalid")
    return hashlib.sha256(b"projectbuddy/host-identity@1\0" + raw).hexdigest()


def _utc(value: datetime | str | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise LivePolicyError("policy timestamp is invalid") from None
    if value.tzinfo is None:
        raise LivePolicyError("policy timestamp must include a timezone")
    return value.astimezone(timezone.utc)


def _require_provisioning_lock(lock: object) -> ProvisioningLock:
    if not isinstance(lock, ProvisioningLock) or not lock.acquired:
        raise LivePolicyError("host provisioning lock must be held across inventory and UUID binding")
    return lock


def _reject_sensitive_fields(value: object) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str) and any(marker in re.sub(r"[^a-z]", "", key.lower()) for marker in SENSITIVE_FIELD_MARKERS):
                raise LivePolicyError("live-run policy cannot contain secret-bearing fields")
            _reject_sensitive_fields(child)
    elif isinstance(value, list):
        for child in value:
            _reject_sensitive_fields(child)


def _stamp(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _uuid(value: object) -> str:
    if not isinstance(value, str):
        raise LivePolicyError("VM identity is invalid")
    try:
        return str(UUID(value))
    except ValueError:
        raise LivePolicyError("VM identity is invalid") from None


def _inventory(values: object) -> list[str]:
    if not isinstance(values, list):
        raise LivePolicyError("VM inventory is invalid")
    result = [_uuid(value) for value in values]
    if len(set(result)) != len(result):
        raise LivePolicyError("VM inventory contains duplicate identities")
    return sorted(result)


def _self_hashed(record: object, keys: set[str], hash_key: str, schema: str) -> dict:
    if not isinstance(record, dict) or set(record) != keys or record.get("schema") != schema:
        raise LivePolicyError("live-run record fields are invalid")
    core = {key: value for key, value in record.items() if key != hash_key}
    if not isinstance(record.get(hash_key), str) or sha256(core) != record[hash_key]:
        raise LivePolicyError("live-run record integrity check failed")
    return record


def _authorization(policy: dict, run_id: str, vm_uuid: str, issued_at: datetime) -> dict:
    template = policy["authorizationTemplate"]
    value = {
        "schema": "projectbuddy/assisted-benchmark-authorization@1",
        "runId": run_id,
        "workload": template["workload"],
        "sourceRevision": policy["sourceRevision"],
        "approvedBy": f"deterministic-policy:{policy['policyId']}",
        "approvedAt": _stamp(issued_at),
        "approvedCalls": template["approvedCalls"],
        "targetVmUuids": [vm_uuid],
        "ollamaModel": template["ollamaModel"],
        "evidenceFields": template["evidenceFields"],
        "allowVmLifecycle": True,
        "teardownOnNativePplFailure": False,
        **{key: template[key] for key in ("reviewBudget", "reviewPrivacy", "reviewLifecycle", "reviewExecution")},
    }
    return value


def _provisioning_request(policy: dict, run_id: str) -> dict:
    provision = policy["provisioning"]
    return {
        "hostProfile": provision["hostProfile"],
        "guestOs": provision["guestOs"],
        "baseImageSha256": provision["baseImageSha256"],
        "vmName": f"{provision['vmNamePrefix']}-{run_id.replace('-', '')[:12]}",
        "vcpus": provision["vcpus"],
        "memoryMb": provision["memoryMb"],
        "diskGb": provision["diskGb"],
    }


def validate_live_policy(policy: object, contract: dict, trusted_policy_sha256: str,
                         now: datetime | str | None = None) -> dict:
    _reject_sensitive_fields(policy)
    if (not isinstance(policy, dict)
            or set(policy) != {"schema", "policyId", "sourceRevision", "provisioning", "authorizationTemplate"}
            or policy["schema"] != POLICY_SCHEMA):
        raise LivePolicyError("live-run policy fields or schema are invalid")
    if (not isinstance(policy["policyId"], str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", policy["policyId"])):
        raise LivePolicyError("live-run policy ID is invalid")
    if not isinstance(trusted_policy_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", trusted_policy_sha256):
        raise LivePolicyError("trusted policy digest is invalid")
    if sha256(policy) != trusted_policy_sha256:
        raise LivePolicyError("live-run policy does not match the digest pinned by the runner")
    if policy["sourceRevision"] != contract.get("source", {}).get("revision"):
        raise LivePolicyError("live-run policy source revision differs from the pinned contract")
    provision = policy["provisioning"]
    if not isinstance(provision, dict) or set(provision) != PROVISIONING_KEYS or provision["maxVms"] != 1:
        raise LivePolicyError("provisioning policy must describe exactly one VM")
    if (not isinstance(provision["hostIdentitySha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", provision["hostIdentitySha256"])
            or not isinstance(provision["hostProfile"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", provision["hostProfile"])
            or provision["guestOs"] not in {"windows", "ubuntu"}
            or not isinstance(provision["baseImageSha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", provision["baseImageSha256"])
            or not isinstance(provision["vmNamePrefix"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,39}", provision["vmNamePrefix"])):
        raise LivePolicyError("provisioning profile identity is invalid")
    for key, low, high in (("vcpus", 1, 64), ("memoryMb", 512, 262144), ("diskGb", 10, 4096)):
        if type(provision[key]) is not int or not low <= provision[key] <= high:
            raise LivePolicyError("provisioning resource bounds are invalid")
    template = policy["authorizationTemplate"]
    if not isinstance(template, dict) or set(template) != AUTHORIZATION_TEMPLATE_KEYS:
        raise LivePolicyError("authorization template fields are invalid")
    issued_at = _utc(now)
    probe_uuid = "00000000-0000-4000-8000-000000000001"
    try:
        validate_authorization(_authorization(policy, "00000000-0000-4000-8000-000000000002", probe_uuid, issued_at), contract)
    except ContractError as error:
        raise LivePolicyError(f"authorization template is invalid: {error}") from None
    return json.loads(canonical_json(policy))


def compile_provision_permit(policy: object, contract: dict, run_id: str,
                             inventory_before: object, trusted_policy_sha256: str,
                             host_lock: ProvisioningLock,
                             now: datetime | str | None = None) -> dict:
    _require_provisioning_lock(host_lock)
    normalized = validate_live_policy(policy, contract, trusted_policy_sha256, now)
    current_host = host_identity_sha256()
    if normalized["provisioning"]["hostIdentitySha256"] != current_host:
        raise LivePolicyError("live-run policy is bound to a different host identity")
    run_uuid = _uuid(run_id)
    if run_uuid != run_id.lower():
        raise LivePolicyError("run nonce must use canonical UUID text")
    issued_at = _utc(now)
    before = _inventory(inventory_before)
    lifetime = min(MAX_POLICY_LIFETIME_SECONDS, normalized["authorizationTemplate"]["reviewLifecycle"]["totalSeconds"])
    expires_at = issued_at + timedelta(seconds=lifetime)
    privacy_expiry = _utc(normalized["authorizationTemplate"]["reviewPrivacy"]["expiresAt"])
    if privacy_expiry < expires_at:
        raise LivePolicyError("privacy approval expires before the compiled run permit")
    create_request = _provisioning_request(normalized, run_uuid)
    core = {
        "schema": PROVISION_SCHEMA,
        "runId": run_uuid,
        "policyId": normalized["policyId"],
        "policySha256": sha256(normalized),
        "hostIdentitySha256": current_host,
        "contractSha256": sha256(contract),
        "issuedAt": _stamp(issued_at),
        "expiresAt": _stamp(expires_at),
        "inventoryBeforeUuids": before,
        "inventoryBeforeSha256": sha256(before),
        "createRequest": create_request,
        "createRequestSha256": sha256(create_request),
        "maxNewVmCount": 1,
    }
    return {**core, "permitSha256": sha256(core)}


def validate_provision_permit(permit: object, policy: dict, contract: dict, trusted_policy_sha256: str,
                              now: datetime | str | None = None) -> dict:
    keys = {"schema", "runId", "policyId", "policySha256", "hostIdentitySha256", "contractSha256", "issuedAt", "expiresAt",
            "inventoryBeforeUuids", "inventoryBeforeSha256", "createRequest", "createRequestSha256", "maxNewVmCount", "permitSha256"}
    value = _self_hashed(permit, keys, "permitSha256", PROVISION_SCHEMA)
    normalized_policy = validate_live_policy(policy, contract, trusted_policy_sha256, now)
    current_host = host_identity_sha256()
    if (value["policySha256"] != trusted_policy_sha256 or value["hostIdentitySha256"] != current_host
            or normalized_policy["provisioning"]["hostIdentitySha256"] != current_host
            or value["contractSha256"] != sha256(contract)
            or value["policyId"] != normalized_policy["policyId"]
            or value["createRequest"] != _provisioning_request(normalized_policy, value["runId"])):
        raise LivePolicyError("provision permit is not derived from the trusted policy")
    _uuid(value["runId"])
    before = _inventory(value["inventoryBeforeUuids"])
    if before != value["inventoryBeforeUuids"] or sha256(before) != value["inventoryBeforeSha256"]:
        raise LivePolicyError("provision permit inventory binding is invalid")
    if sha256(value["createRequest"]) != value["createRequestSha256"] or value["maxNewVmCount"] != 1:
        raise LivePolicyError("provision permit creation scope is invalid")
    if not re.fullmatch(r"[a-f0-9]{64}", value["policySha256"]) or not re.fullmatch(r"[a-f0-9]{64}", value["contractSha256"]):
        raise LivePolicyError("provision permit digest is invalid")
    if _utc(value["issuedAt"]) >= _utc(value["expiresAt"]) or _utc(now) >= _utc(value["expiresAt"]):
        raise LivePolicyError("provision permit is expired or has invalid time bounds")
    return value


def claim_provision_permit_once(permit: object, policy: dict, contract: dict, trusted_policy_sha256: str,
                                ledger_directory: Path, host_lock: ProvisioningLock,
                                now: datetime | str | None = None) -> Path:
    lock = _require_provisioning_lock(host_lock)
    if Path(ledger_directory).resolve(strict=True) != lock.directory:
        raise LivePolicyError("provision permit ledger differs from the locked host directory")
    value = validate_provision_permit(permit, policy, contract, trusted_policy_sha256, now)
    if os.name != "posix":
        raise LivePolicyError("one-use permit ledger custody is unsupported on this platform")
    directory = Path(ledger_directory)
    if not directory.is_absolute() or directory.is_symlink() or directory.resolve(strict=True) != directory:
        raise LivePolicyError("permit ledger directory must be absolute and non-symlinked")
    state = directory.stat()
    if state.st_uid != os.getuid() or stat.S_IMODE(state.st_mode) != 0o700:
        raise LivePolicyError("permit ledger directory requires private current-user custody")
    marker = directory / f"{value['runId']}.permit"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        descriptor = os.open(marker, flags, 0o600)
    except FileExistsError:
        raise LivePolicyError("provision permit has already been claimed") from None
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(canonical_json({"runId": value["runId"], "permitSha256": value["permitSha256"]}) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        raise LivePolicyError("provision permit could not be durably claimed") from None
    return marker


def claim_run_grant_once(grant: object, policy: dict, contract: dict, trusted_policy_sha256: str,
                         ledger_directory: Path, now: datetime | str | None = None) -> Path:
    value = validate_run_grant(grant, policy, contract, trusted_policy_sha256, now)
    directory = Path(ledger_directory)
    if (os.name != "posix" or not directory.is_absolute() or directory.is_symlink()
            or directory.resolve(strict=True) != directory):
        raise LivePolicyError("run grant ledger requires an absolute private POSIX directory")
    state = directory.stat()
    if state.st_uid != os.getuid() or stat.S_IMODE(state.st_mode) != 0o700:
        raise LivePolicyError("run grant ledger requires private current-user custody")
    marker = directory / f"{value['runId']}.run"
    descriptor = -1
    try:
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(canonical_json({"runId": value["runId"], "grantSha256": value["grantSha256"]}) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except FileExistsError:
        raise LivePolicyError("sealed run grant has already been claimed") from None
    except OSError:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise LivePolicyError("sealed run grant could not be durably claimed") from None
    return marker


def _validate_permit_claim(permit: dict, claim_path: Path) -> str:
    marker = Path(claim_path)
    parent = marker.parent
    if not marker.is_absolute() or marker.name != f"{permit['runId']}.permit" or marker.is_symlink():
        raise LivePolicyError("one-use permit claim is missing or unsafe")
    try:
        directory = parent.stat()
        descriptor = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as handle:
            state = os.fstat(handle.fileno())
            if (parent.resolve(strict=True) != parent or directory.st_uid != os.getuid()
                    or stat.S_IMODE(directory.st_mode) != 0o700 or state.st_uid != os.getuid()
                    or stat.S_IMODE(state.st_mode) != 0o600 or state.st_nlink != 1 or not stat.S_ISREG(state.st_mode)):
                raise LivePolicyError("one-use permit claim custody is unsafe")
            actual = handle.read(4097)
    except OSError:
        raise LivePolicyError("one-use permit claim is missing or unsafe") from None
    expected = canonical_json({"runId": permit["runId"], "permitSha256": permit["permitSha256"]}) + b"\n"
    if actual != expected:
        raise LivePolicyError("one-use permit claim does not match the compiled permit")
    return hashlib.sha256(actual).hexdigest()


def bind_created_vm(policy: object, permit: object, contract: dict, inventory_after: object,
                    provisioner_uuid: str, trusted_policy_sha256: str, claim_path: Path,
                    host_lock: ProvisioningLock,
                    now: datetime | str | None = None) -> dict:
    lock = _require_provisioning_lock(host_lock)
    if Path(claim_path).parent != lock.directory:
        raise LivePolicyError("provision permit claim differs from the locked host directory")
    normalized = validate_live_policy(policy, contract, trusted_policy_sha256, now)
    value = validate_provision_permit(permit, normalized, contract, trusted_policy_sha256, now)
    claim_sha256 = _validate_permit_claim(value, claim_path)
    current_host = host_identity_sha256()
    if normalized["provisioning"]["hostIdentitySha256"] != current_host:
        raise LivePolicyError("provision permit is bound to a different host identity")
    if value["policySha256"] != sha256(normalized) or value["contractSha256"] != sha256(contract):
        raise LivePolicyError("provision permit does not match the live policy and MCP contract")
    if value["policyId"] != normalized["policyId"]:
        raise LivePolicyError("provision permit policy ID does not match")
    returned_uuid = _uuid(provisioner_uuid)
    after = _inventory(inventory_after)
    before = set(value["inventoryBeforeUuids"])
    if not before.issubset(after):
        raise LivePolicyError("VM inventory lost an existing identity during provisioning")
    new_uuids = set(after) - before
    if len(new_uuids) != 1 or returned_uuid not in new_uuids:
        raise LivePolicyError("provisioner result does not identify exactly one newly created VM")
    issued_at = _utc(value["issuedAt"])
    authorization = _authorization(normalized, value["runId"], returned_uuid, issued_at)
    try:
        authorization = validate_authorization(authorization, contract)
    except ContractError as error:
        raise LivePolicyError(f"sealed run authorization is invalid: {error}") from None
    core = {
        "schema": GRANT_SCHEMA,
        "runId": value["runId"],
        "policyId": normalized["policyId"],
        "policySha256": value["policySha256"],
        "hostIdentitySha256": current_host,
        "contractSha256": value["contractSha256"],
        "permitSha256": value["permitSha256"],
        "permitClaimSha256": claim_sha256,
        "provisionPermit": value,
        "vmUuid": returned_uuid,
        "issuedAt": value["issuedAt"],
        "expiresAt": value["expiresAt"],
        "inventoryAfterUuids": after,
        "inventoryAfterSha256": sha256(after),
        "authorization": authorization,
        "teardown": {"vmUuid": returned_uuid, "on": ["success", "failure"], "maxAttempts": 1},
    }
    return {**core, "grantSha256": sha256(core)}


def validate_run_grant(grant: object, policy: dict, contract: dict, trusted_policy_sha256: str,
               now: datetime | str | None = None) -> dict:
    keys = {"schema", "runId", "policyId", "policySha256", "hostIdentitySha256", "contractSha256", "permitSha256", "permitClaimSha256", "vmUuid",
        "provisionPermit", "issuedAt", "expiresAt", "inventoryAfterUuids", "inventoryAfterSha256",
        "authorization", "teardown", "grantSha256"}
    value = _self_hashed(grant, keys, "grantSha256", GRANT_SCHEMA)
    normalized_policy = validate_live_policy(policy, contract, trusted_policy_sha256, now)
    permit = validate_provision_permit(value["provisionPermit"], normalized_policy, contract, trusted_policy_sha256, now)
    expected_claim = hashlib.sha256(canonical_json({"runId": permit["runId"], "permitSha256": permit["permitSha256"]}) + b"\n").hexdigest()
    current_host = host_identity_sha256()
    vm_uuid = _uuid(value["vmUuid"])
    after = _inventory(value["inventoryAfterUuids"])
    if after != value["inventoryAfterUuids"] or sha256(after) != value["inventoryAfterSha256"] or vm_uuid not in after:
        raise LivePolicyError("run grant inventory binding is invalid")
    created = set(after) - set(permit["inventoryBeforeUuids"])
    if (value["policySha256"] != trusted_policy_sha256 or value["hostIdentitySha256"] != current_host
            or normalized_policy["provisioning"]["hostIdentitySha256"] != current_host
            or value["policyId"] != normalized_policy["policyId"]
            or value["contractSha256"] != sha256(contract) or value["permitSha256"] != permit["permitSha256"]
            or value["permitClaimSha256"] != expected_claim
            or value["runId"] != permit["runId"] or value["issuedAt"] != permit["issuedAt"]
            or value["expiresAt"] != permit["expiresAt"] or created != {vm_uuid}):
        raise LivePolicyError("run grant does not match its trusted policy and provision permit")
    expected_authorization = _authorization(normalized_policy, value["runId"], vm_uuid, _utc(value["issuedAt"]))
    try:
        authorization = validate_authorization(value["authorization"], contract)
    except ContractError as error:
        raise LivePolicyError(f"run grant authorization is invalid: {error}") from None
    if authorization != expected_authorization:
        raise LivePolicyError("run grant authorization differs from the trusted policy")
    if value["teardown"] != {"vmUuid": vm_uuid, "on": ["success", "failure"], "maxAttempts": 1}:
        raise LivePolicyError("run grant teardown binding is invalid")
    if _utc(value["issuedAt"]) >= _utc(value["expiresAt"]) or _utc(now) >= _utc(value["expiresAt"]):
        raise LivePolicyError("run grant is expired or has invalid time bounds")
    return value


class LiveRunBroker:
    def __init__(self, grant: dict, policy: dict, contract: dict, trusted_policy_sha256: str,
                 run_ledger_directory: Path, clock=None):
        self.contract = contract
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.grant = validate_run_grant(grant, policy, contract, trusted_policy_sha256, self.clock())
        self.run_claim = claim_run_grant_once(grant, policy, contract, trusted_policy_sha256,
                                              run_ledger_directory, self.clock())
        self.authorization = self.grant["authorization"]
        self.calls = self.authorization["approvedCalls"]
        self.next_index = 0
        self.in_flight = False
        self.terminal = False
        self.teardown_issued = False

    def _deny(self, message: str):
        self.terminal = True
        self.in_flight = False
        raise LivePolicyError(message)

    def begin_call(self, tool: str, arguments: object, vm_uuid: str) -> dict:
        if self.terminal or self.in_flight:
            raise LivePolicyError("run grant is terminal or has an in-flight call")
        try:
            current_uuid = _uuid(vm_uuid)
        except LivePolicyError:
            self._deny("MCP call VM identity does not match the run grant")
        if current_uuid != self.grant["vmUuid"] or _utc(self.clock()) >= _utc(self.grant["expiresAt"]):
            self._deny("MCP call is outside the run grant identity or time bound")
        if self.next_index >= len(self.calls):
            self._deny("run grant has no remaining MCP calls")
        expected = self.calls[self.next_index]
        try:
            proposed = canonical_json({"tool": tool, "arguments": arguments})
        except LivePolicyError:
            self._deny("MCP call is not canonical JSON")
        if proposed != canonical_json(expected):
            self._deny("MCP call differs from the next exact run grant entry")
        try:
            decision = authorize_tool_call(tool, arguments, self.authorization, self.contract)
        except ContractError as error:
            self._deny(f"MCP call failed pinned contract authorization: {error}")
        self.in_flight = True
        return {**decision, "callIndex": self.next_index, "vmUuid": current_uuid, "grantSha256": self.grant["grantSha256"]}

    def complete_call(self, succeeded: bool) -> None:
        if not self.in_flight or type(succeeded) is not bool:
            raise LivePolicyError("run grant has no matching in-flight call")
        self.in_flight = False
        if not succeeded:
            self.terminal = True
            return
        self.next_index += 1
        if self.next_index == len(self.calls):
            self.terminal = True

    def abort(self) -> None:
        self.in_flight = False
        self.terminal = True

    def teardown_target(self) -> dict:
        if not self.terminal or self.in_flight:
            raise LivePolicyError("teardown is available only after run termination")
        if self.teardown_issued:
            raise LivePolicyError("exact-UUID teardown capability was already issued")
        self.teardown_issued = True
        return {"vmUuid": self.grant["vmUuid"], "maxAttempts": 1, "grantSha256": self.grant["grantSha256"]}