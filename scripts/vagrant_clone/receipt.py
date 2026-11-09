"""Clone receipt (``projectbuddy/ubuntu-vagrant-clone@2``) and its verifier.

The verifier rebuilds the whole receipt from the profile, the evidence, the source box receipt, and
the clone's identity; any difference fails. It also verifies ``projectbuddy/ubuntu-vagrant-clone@1``
receipts, which name no profile: only against a profile whose SHA-256 the caller pins and that
declares the ``@1`` runtime block.
"""
from __future__ import annotations

import pathlib

from lane_box.common import SHA256_PATTERN, LaneBoxError, canonical_uuid, forbidden_keys, sha256_file
from lane_box.readiness import RUNNER_UNIT

from .profile import Profile, ProfileError, boot_id, failures, load_evidence, parse_json, same

RECEIPT_SCHEMA = "projectbuddy/ubuntu-vagrant-clone@2"
RECEIPT_V1_SCHEMA = "projectbuddy/ubuntu-vagrant-clone@1"
RECEIPT_FILENAME = "vagrant-clone-receipt.json"
FINAL_STATE = "poweroff"


class ReceiptError(LaneBoxError):
    pass


def load_object(path: pathlib.Path, what: str) -> dict:
    try:
        value = parse_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise ReceiptError(f"{what} is unreadable: {error}") from error
    if not isinstance(value, dict):
        raise ReceiptError(f"{what} must be a JSON object")
    return value


def source_box(path: pathlib.Path) -> dict:
    """The ``sourceBox`` block: the box receipt's digest and the box digest it records."""
    box = load_object(path, "box receipt").get("box")
    sha256 = box.get("sha256") if isinstance(box, dict) else None
    if not isinstance(sha256, str) or not SHA256_PATTERN.fullmatch(sha256):
        raise ReceiptError("box receipt does not record a box SHA-256")
    return {"receiptSha256": sha256_file(path), "boxSha256": sha256}


def _clone(name: str, uuid: str, boot: str) -> dict:
    return {"vm": {"name": name, "uuid": uuid}, "bootId": boot, "finalState": FINAL_STATE}


def build_receipt(profile: Profile, *, box: dict, name: str, uuid: str, boot: str, evidence_dir: pathlib.Path,
                  observed: list[str]) -> dict:
    return {
        "schema": RECEIPT_SCHEMA,
        "profile": {"name": profile.name, "sha256": profile.sha256},
        "sourceBox": box,
        "clone": _clone(name, uuid, boot),
        "runnerUnits": {"policy": profile.runner_units, "observed": observed},
        "evidenceSha256": {file: sha256_file(evidence_dir / file) for file in profile.files},
        "failures": [],
        "outcome": "PASS",
    }


def build_v1_receipt(profile: Profile, *, box: dict, name: str, uuid: str, boot: str,
                     evidence_dir: pathlib.Path) -> dict:
    return {
        "schema": RECEIPT_V1_SCHEMA,
        "sourceBox": box,
        "clone": _clone(name, uuid, boot),
        "evidenceSha256": {pathlib.PurePath(file).stem: sha256_file(evidence_dir / file) for file in profile.files},
        "runtime": profile.receipt_v1_runtime,
        "failures": [],
        "outcome": "PASS",
    }


def _observed(receipt: dict, profile: Profile, errors: list[str]) -> list[str] | None:
    block = receipt.get("runnerUnits")
    observed = block.get("observed") if isinstance(block, dict) else None
    if (not isinstance(observed, list) or not all(isinstance(unit, str) and RUNNER_UNIT.fullmatch(unit)
                                                  for unit in observed) or observed != sorted(set(observed))):
        errors.append("runner units must be a sorted list of distinct actions.runner.* units")
        return None
    if observed and profile.runner_units == "refuse":
        errors.append("the profile refuses Actions runner units, but the receipt records some")
    return observed


def verify_receipt(path: pathlib.Path, *, profile: Profile, box_receipt: pathlib.Path, evidence_dir: pathlib.Path,
                   name: str, uuid: str, v1_profile_sha256: str | None = None) -> dict:
    """Return the verified receipt or raise ``ReceiptError`` naming every failed check."""
    receipt = load_object(path, "clone receipt")
    uuid = canonical_uuid(uuid, "clone VM UUID")
    box = source_box(box_receipt)
    evidence = load_evidence(profile, evidence_dir)
    errors = [f"raw capture: {message}" for message in failures(profile, evidence)]
    try:
        boot = boot_id(profile, evidence)
    except ProfileError as error:
        errors.append(str(error))
        boot = None
    found = forbidden_keys(receipt)
    if found:
        errors.append(f"forbidden fields present: {', '.join(found)}")
    schema = receipt.get("schema")
    expected = None
    if schema == RECEIPT_SCHEMA:
        observed = _observed(receipt, profile, errors)
        if observed is not None:
            expected = build_receipt(profile, box=box, name=name, uuid=uuid, boot=boot, evidence_dir=evidence_dir,
                                     observed=observed)
    elif schema == RECEIPT_V1_SCHEMA:
        if profile.sha256 != v1_profile_sha256 or profile.receipt_v1_runtime is None:
            errors.append(f"a {RECEIPT_V1_SCHEMA} receipt verifies only against its pinned profile")
        else:
            expected = build_v1_receipt(profile, box=box, name=name, uuid=uuid, boot=boot, evidence_dir=evidence_dir)
    else:
        errors.append("unexpected schema")
    if expected is not None:
        differing = sorted(key for key in set(expected) | set(receipt) if key not in expected or key not in receipt
                           or not same(expected[key], receipt[key]))
        if differing:
            errors.append(f"receipt does not match its inputs: {', '.join(differing)}")
    if errors:
        raise ReceiptError("; ".join(errors))
    return receipt
