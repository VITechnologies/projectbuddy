#!/usr/bin/env python3
import argparse
import hashlib
import json
import pathlib
import sys
import uuid


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("receipt", type=pathlib.Path)
    parser.add_argument("--manifest", type=pathlib.Path, required=True)
    parser.add_argument("--vm-name", required=True)
    parser.add_argument("--vm-uuid", required=True)
    arguments = parser.parse_args()

    try:
        expected_uuid = str(uuid.UUID(arguments.vm_uuid))
        manifest_bytes = arguments.manifest.read_bytes()
        manifest = json.loads(manifest_bytes)
        receipt = json.loads(arguments.receipt.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"public provisioning receipt: FAIL: {error}", file=sys.stderr)
        return 2

    errors = []

    def require(condition: bool, message: str) -> None:
        if not condition:
            errors.append(message)

    require(
        receipt.get("schema") == "sandbox-sbom-reporting/ubuntu-public-provisioning@1",
        "unexpected schema",
    )
    require(receipt.get("manifestSha256") == hashlib.sha256(manifest_bytes).hexdigest(), "manifest hash mismatch")
    require(receipt.get("os") == {
        "distribution": manifest.get("os", {}).get("distribution"),
        "version": manifest.get("os", {}).get("version"),
        "codename": manifest.get("os", {}).get("codename"),
    }, "guest OS does not match manifest")
    require(receipt.get("outcome") == "PASS", "outcome is not PASS")
    require(receipt.get("failures") == [], "receipt contains failures")

    vm = receipt.get("vm", {})
    require(vm.get("name") == arguments.vm_name, "VM name does not match expected identity")
    try:
        actual_uuid = str(uuid.UUID(str(vm.get("uuid"))))
    except (ValueError, AttributeError):
        errors.append("VM UUID is invalid")
    else:
        require(actual_uuid == expected_uuid, "VM UUID does not match expected identity")

    labview = manifest.get("labview", {})
    require(receipt.get("labview") == {
        "edition": labview.get("edition"),
        "package": labview.get("package"),
        "packageVersion": labview.get("packageVersion"),
        "installPath": labview.get("installPath"),
        "executableBasenames": labview.get("executableBasenames"),
        "viServerPort": labview.get("viServerPort"),
    }, "LabVIEW installation does not match manifest")
    vipm = manifest.get("vipm", {})
    require(receipt.get("vipm") == {
        "package": "vipm",
        "packageVersion": vipm.get("version"),
    }, "VIPM installation does not match manifest")

    if errors:
        for error in errors:
            print(f"public provisioning receipt: FAIL: {error}", file=sys.stderr)
        return 2
    print(f"public provisioning receipt: PASS: {arguments.vm_name} ({expected_uuid})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())