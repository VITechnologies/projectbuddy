"""Lane box receipt (``projectbuddy/ubuntu-lane-box@2``) and its verifier.

The verifier recomputes the whole receipt from the box, the profile, the base and
readiness receipts, and the embedded Vagrantfile; any difference fails.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import tarfile

from .access import SSHD_EXPECTED, UNIT_LINK, UNIT_LINK_TARGET, expected_files
from .common import VM_NAME_PATTERN, LaneBoxError, canonical_uuid, forbidden_keys, sha256_file
from .profile import Profile, load_profile
from .readiness import NOBLE, READINESS_SCHEMA, SKIPPED_ROOTS

BOX_SCHEMA = "projectbuddy/ubuntu-lane-box@2"
BASE_SCHEMA = "projectbuddy/ubuntu-base-bootstrap@2"
FINGERPRINT_PATTERN = re.compile(r"SHA256:[A-Za-z0-9+/]{20,}={0,2}")
# The Vagrantfile that `vagrant package --vagrantfile` generates; anything else is unexpected behavior.
WRAPPER_PATTERN = re.compile(
    r'Vagrant::Config\.run do \|config\|\n'
    r'(?:  #.*\n){3}'
    r'  config\.vm\.base_mac = "[0-9A-F]{12}"\nend\n\n'
    r'# Load include vagrant file if it exists after the auto-generated\n'
    r'# so it can override any of the settings\n'
    r'include_vagrantfile = File\.expand_path\("\.\./include/_Vagrantfile", __FILE__\)\n'
    r'load include_vagrantfile if File\.exist\?\(include_vagrantfile\)\n?'
)


def load_json(path: pathlib.Path, what: str) -> dict:
    if not path.is_file():
        raise LaneBoxError(f"{what} is missing: {path.name}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LaneBoxError(f"{what} is unreadable: {error}") from error
    if not isinstance(value, dict):
        raise LaneBoxError(f"{what} must be a JSON object")
    return value


def check_base_receipt(receipt: dict, vm_name: str, vm_uuid: str) -> list[str]:
    """The same checks as vm/ubuntu-labview/verify-base-receipt.py, with the fingerprint read from the receipt."""
    errors: list[str] = []

    def require(condition: bool, message: str) -> None:
        if not condition:
            errors.append(f"base receipt: {message}")

    require(receipt.get("schema") == BASE_SCHEMA, "unexpected schema")
    require(receipt.get("os") == NOBLE, "unexpected guest OS")
    require(receipt.get("outcome") == "PASS", "outcome is not PASS")
    require(receipt.get("failures") == [], "receipt contains failures")
    vm = receipt.get("vm") if isinstance(receipt.get("vm"), dict) else {}
    require(vm.get("name") == vm_name, "VM name does not match")
    try:
        require(canonical_uuid(vm.get("uuid"), "VM UUID") == vm_uuid, "VM UUID does not match")
    except LaneBoxError:
        errors.append("base receipt: VM UUID is invalid")
    key = receipt.get("sshHostKey") if isinstance(receipt.get("sshHostKey"), dict) else {}
    require(key.get("algorithm") == "ssh-ed25519", "unexpected SSH host-key algorithm")
    require(isinstance(key.get("fingerprint"), str) and bool(FINGERPRINT_PATTERN.fullmatch(key["fingerprint"])),
            "SSH host fingerprint is invalid")
    tools = receipt.get("tools") if isinstance(receipt.get("tools"), dict) else {}
    for name in ("sshd", "virtualBoxGuestService"):
        tool = tools.get(name) if isinstance(tools.get(name), dict) else {}
        require(isinstance(tool.get("path"), str) and tool["path"].startswith("/"), f"{name} path is missing")
        require(isinstance(tool.get("version"), str) and bool(tool["version"]), f"{name} version is missing")
    services = receipt.get("services") if isinstance(receipt.get("services"), dict) else {}
    for name in ("ssh", "virtualBoxGuestUtils"):
        service = services.get(name) if isinstance(services.get(name), dict) else {}
        require(service.get("activeState") == "active", f"{name} is not active")
        require(service.get("enabledState") == "enabled", f"{name} is not enabled")
    return errors


def check_readiness_receipt(receipt: dict, profile: Profile, vm_name: str, vm_uuid: str) -> list[str]:
    errors: list[str] = []

    def require(condition: bool, message: str) -> None:
        if not condition:
            errors.append(f"readiness receipt: {message}")

    require(receipt.get("schema") == READINESS_SCHEMA, "unexpected schema")
    require(receipt.get("outcome") == "PASS", "outcome is not PASS")
    require(receipt.get("failures") == [], "receipt contains failures")
    require(receipt.get("profile") == {"name": profile.name, "sha256": profile.sha256},
            "was produced for a different lane profile")
    require(receipt.get("vm") == {"name": vm_name, "uuid": vm_uuid}, "VM identity does not match")
    try:
        canonical_uuid(receipt.get("bootId"), "boot id")
    except LaneBoxError:
        errors.append("readiness receipt: boot id is invalid")
    runner = receipt.get("runnerAbsence") if isinstance(receipt.get("runnerAbsence"), dict) else {}
    require(runner.get("runnerConfigurations") == [], "an Actions runner configuration is present")
    require(runner.get("runnerUnits") == [], "an Actions runner unit is present")
    require(runner.get("scanErrors") == [], "the filesystem scan was incomplete")
    require(runner.get("skippedRoots") == ["/" + name for name in SKIPPED_ROOTS], "unexpected skipped roots")
    labview = receipt.get("labviewAbsence") if isinstance(receipt.get("labviewAbsence"), dict) else {}
    require(labview == {"paths": [], "packages": [], "scanErrors": []}, "LabVIEW, VIPM, or NI residue is present")
    observed = receipt.get("toolchain") if isinstance(receipt.get("toolchain"), list) else []
    expected = [{"name": tool.name, "expected": tool.probe.expected, "observed": tool.probe.expected,
                 "exitCode": 0, "ok": True} for tool in profile.tools]
    require(observed == expected, "toolchain probes do not match the profile")
    access = receipt.get("laneAccess") if isinstance(receipt.get("laneAccess"), dict) else {}
    require(access.get("files") == expected_files(), "lane access files do not match this engine")
    require(access.get("unitLink") == {"path": "/" + UNIT_LINK, "target": UNIT_LINK_TARGET},
            "the lane access unit is not enabled")
    sshd = access.get("sshd") if isinstance(access.get("sshd"), list) else []
    users = [entry.get("user") for entry in sshd if isinstance(entry, dict)]
    require("root" in users and users == sorted(set(users), key=str),
            "sshd was not checked for root and each account once")
    passing = [{"user": user, "exitCode": 0, "effective": SSHD_EXPECTED} for user in users]
    require(sshd == passing, "sshd does not enforce lane access")
    require(access.get("authorizedKeys") == [], "an authorized_keys file is present")
    require(access.get("scanErrors") == [], "the lane access scan was incomplete")
    return errors


def check_box_archive(box: pathlib.Path) -> str:
    """Return the SHA-256 of the box's included Vagrantfile after checking the generated wrapper."""
    try:
        with tarfile.open(box, "r:*") as archive:
            members = archive.getmembers()
            wrappers = [member for member in members if member.name.lstrip("./") == "Vagrantfile"]
            policies = [member for member in members if member.name.lstrip("./") == "include/_Vagrantfile"]
            if len(wrappers) != 1 or not wrappers[0].isfile():
                raise LaneBoxError("box must contain exactly one regular generated Vagrantfile")
            if len(policies) != 1 or not policies[0].isfile():
                raise LaneBoxError("box must contain exactly one regular included Vagrantfile")
            wrapper_stream = archive.extractfile(wrappers[0])
            policy_stream = archive.extractfile(policies[0])
            wrapper = wrapper_stream.read().decode("utf-8")
            embedded = hashlib.sha256(policy_stream.read()).hexdigest()
    except (OSError, UnicodeDecodeError, tarfile.TarError) as error:
        raise LaneBoxError(f"box is unreadable: {error}") from error
    if not WRAPPER_PATTERN.fullmatch(wrapper):
        raise LaneBoxError("generated Vagrantfile contains unexpected behavior")
    return embedded


def build_box_receipt(
    *,
    profile: Profile,
    vm: dict,
    snapshot: dict,
    box: pathlib.Path,
    inputs: dict[str, pathlib.Path],
    base: dict,
    readiness: dict,
) -> dict:
    observed = {result["name"]: result["observed"] for result in readiness["toolchain"]}
    return {
        "schema": BOX_SCHEMA,
        "profile": {"name": profile.name, "sha256": profile.sha256},
        "source": {"vm": vm, "snapshot": snapshot},
        "box": {"filename": box.name, "sha256": sha256_file(box), "sizeBytes": box.stat().st_size},
        "provenanceSha256": {name: sha256_file(path) for name, path in sorted(inputs.items())},
        "os": profile.os,
        "virtualBox": profile.virtual_box,
        "toolchain": [dict(entry, observed=observed.get(entry["name"])) for entry in profile.toolchain_block()],
        "runnerAbsence": {
            "checkedBootId": readiness["bootId"],
            "runnerConfigurations": [],
            "runnerUnits": [],
            "skippedRoots": ["/" + name for name in SKIPPED_ROOTS],
        },
        "labviewAbsence": {"paths": [], "packages": []},
        "laneAccess": {
            "files": [{"path": entry["path"], "sha256": entry["sha256"]} for entry in expected_files()],
            "checkedUsers": [entry["user"] for entry in readiness["laneAccess"]["sshd"]],
        },
        "sshHostKey": base["sshHostKey"],
        "failures": [],
        "outcome": "PASS",
    }


def verify_box_receipt(
    receipt_path: pathlib.Path,
    *,
    box: pathlib.Path,
    profile_path: pathlib.Path,
    base_receipt: pathlib.Path,
    readiness_receipt: pathlib.Path,
    embedded_vagrantfile: pathlib.Path,
    expected_vm: dict | None = None,
    expected_snapshot: dict | None = None,
) -> dict:
    """Return the verified receipt or raise ``LaneBoxError`` naming every failed check."""
    if os.name != "nt":
        for path, what in ((receipt_path, "receipt"), (box, "box")):
            if path.is_file() and path.stat().st_mode & 0o777 != 0o600:
                raise LaneBoxError(f"{what} mode must be 600")
    receipt = load_json(receipt_path, "lane box receipt")
    if not box.is_file():
        raise LaneBoxError(f"box is missing: {box.name}")
    if not embedded_vagrantfile.is_file():
        raise LaneBoxError(f"embedded Vagrantfile is missing: {embedded_vagrantfile.name}")
    profile = load_profile(profile_path)
    base = load_json(base_receipt, "base receipt")
    readiness = load_json(readiness_receipt, "readiness receipt")
    source = receipt.get("source") if isinstance(receipt.get("source"), dict) else {}
    vm = source.get("vm") if isinstance(source.get("vm"), dict) else {}
    snapshot = source.get("snapshot") if isinstance(source.get("snapshot"), dict) else {}
    errors: list[str] = []
    if expected_vm is not None and vm != expected_vm:
        errors.append("source VM does not match the expected identity")
    if expected_snapshot is not None and snapshot != expected_snapshot:
        errors.append("source snapshot does not match the expected identity")
    if not (isinstance(vm.get("name"), str) and VM_NAME_PATTERN.fullmatch(vm["name"])
            and isinstance(snapshot.get("name"), str) and VM_NAME_PATTERN.fullmatch(snapshot["name"])):
        errors.append("source VM or snapshot name is invalid")
    for value, what in ((vm.get("uuid"), "VM UUID"), (snapshot.get("uuid"), "snapshot UUID")):
        try:
            if canonical_uuid(value, what) != value:
                errors.append(f"source {what} is not canonical")
        except LaneBoxError as error:
            errors.append(f"source {error}")
    errors.extend(check_base_receipt(base, vm.get("name"), vm.get("uuid")))
    errors.extend(check_readiness_receipt(readiness, profile, vm.get("name"), vm.get("uuid")))
    if check_box_archive(box) != sha256_file(embedded_vagrantfile):
        errors.append("the box's included Vagrantfile does not match the embedded Vagrantfile")
    found = forbidden_keys(receipt)
    if found:
        errors.append(f"forbidden fields present: {', '.join(found)}")
    if not errors:
        expected = build_box_receipt(
            profile=profile, vm=vm, snapshot=snapshot, box=box,
            inputs=provenance_inputs(profile_path, base_receipt, readiness_receipt, embedded_vagrantfile),
            base=base, readiness=readiness,
        )
        differing = sorted(key for key in set(expected) | set(receipt) if expected.get(key) != receipt.get(key))
        if differing:
            errors.append(f"receipt does not match its inputs: {', '.join(differing)}")
    if errors:
        raise LaneBoxError("; ".join(errors))
    return receipt


def provenance_inputs(
    profile_path: pathlib.Path,
    base_receipt: pathlib.Path,
    readiness_receipt: pathlib.Path,
    embedded_vagrantfile: pathlib.Path,
) -> dict[str, pathlib.Path]:
    return {
        "baseReceipt": base_receipt,
        "embeddedVagrantfile": embedded_vagrantfile,
        "profile": profile_path,
        "readinessReceipt": readiness_receipt,
    }
