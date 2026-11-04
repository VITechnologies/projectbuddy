#!/usr/bin/env python3
import argparse
import hashlib
import json
import pathlib
import sys
import uuid


PREACTIVATION_SNAPSHOT_NAME = "labview2026-q3-pro-installed-preactivation"


def digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("receipt", type=pathlib.Path)
    parser.add_argument("--base-receipt", type=pathlib.Path, required=True)
    parser.add_argument("--public-receipt", type=pathlib.Path, required=True)
    parser.add_argument("--manifest", type=pathlib.Path, required=True)
    parser.add_argument("--vm-name", required=True)
    parser.add_argument("--vm-uuid", required=True)
    parser.add_argument("--snapshot-name", required=True)
    parser.add_argument("--snapshot-uuid", required=True)
    arguments = parser.parse_args()

    if arguments.snapshot_name != PREACTIVATION_SNAPSHOT_NAME:
        print("public appliance receipt: FAIL: snapshot name does not match the public appliance contract", file=sys.stderr)
        return 2

    try:
        expected_vm_uuid = str(uuid.UUID(arguments.vm_uuid))
        expected_snapshot_uuid = str(uuid.UUID(arguments.snapshot_uuid))
        receipt = json.loads(arguments.receipt.read_text(encoding="utf-8"))
        expected_sources = {
            "baseReceiptSha256": digest(arguments.base_receipt),
            "manifestSha256": digest(arguments.manifest),
            "publicProvisioningReceiptSha256": digest(arguments.public_receipt),
        }
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"public appliance receipt: FAIL: {error}", file=sys.stderr)
        return 2

    errors = []

    def require(condition: bool, message: str) -> None:
        if not condition:
            errors.append(message)

    require(receipt.get("schema") == "sandbox-sbom-reporting/ubuntu-public-appliance@1", "unexpected schema")
    require(receipt.get("outcome") == "PASS", "outcome is not PASS")
    require(receipt.get("vm") == {"name": arguments.vm_name, "uuid": expected_vm_uuid}, "VM identity mismatch")
    require(
        receipt.get("snapshot") == {"name": arguments.snapshot_name, "uuid": expected_snapshot_uuid},
        "snapshot identity mismatch",
    )
    require(receipt.get("sources") == expected_sources, "source hash mismatch")

    if errors:
        for error in errors:
            print(f"public appliance receipt: FAIL: {error}", file=sys.stderr)
        return 2
    print(f"public appliance receipt: PASS: {arguments.vm_name} ({expected_snapshot_uuid})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())