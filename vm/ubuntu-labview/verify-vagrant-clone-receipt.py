#!/usr/bin/env python3
import argparse
import hashlib
import json
import pathlib
import re
import sys
import uuid


SOURCE_FILES = (
    "activation-known-answer-capture.json",
    "sandbox-runtime-state.json",
    "sandbox-vipm-about.json",
    "sandbox-vipm-labview.json",
    "sandbox-vipm-installed.json",
)
FORBIDDEN_FIELDS = {
    "activation_code", "credential", "email", "full_name", "fullname",
    "installation_id", "password", "private_key", "serial", "serial_number", "token",
}


def digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key.lower()
            yield from keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from keys(child)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("receipt", type=pathlib.Path)
    parser.add_argument("--box-receipt", type=pathlib.Path, required=True)
    parser.add_argument("--evidence-dir", type=pathlib.Path, required=True)
    parser.add_argument("--vm-name", required=True)
    parser.add_argument("--vm-uuid", required=True)
    arguments = parser.parse_args()
    try:
        expected_vm_uuid = str(uuid.UUID(arguments.vm_uuid))
        receipt = json.loads(arguments.receipt.read_text(encoding="utf-8"))
        box_receipt = json.loads(arguments.box_receipt.read_text(encoding="utf-8"))
        expected_hashes = {
            pathlib.Path(name).stem: digest(arguments.evidence_dir / name) for name in SOURCE_FILES
        }
        runtime = json.loads((arguments.evidence_dir / "sandbox-runtime-state.json").read_text(encoding="utf-8"))
        probe = json.loads((arguments.evidence_dir / "activation-known-answer-capture.json").read_text(encoding="utf-8"))
        about = json.loads((arguments.evidence_dir / "sandbox-vipm-about.json").read_text(encoding="utf-8"))
        labview = json.loads((arguments.evidence_dir / "sandbox-vipm-labview.json").read_text(encoding="utf-8"))
        installed = json.loads((arguments.evidence_dir / "sandbox-vipm-installed.json").read_text(encoding="utf-8"))
        boot_id = str(uuid.UUID(runtime.get("bootId", "")))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"Vagrant clone receipt: FAIL: {error}", file=sys.stderr)
        return 2

    errors = []

    def require(condition: bool, message: str) -> None:
        if not condition:
            errors.append(message)

    require(receipt.get("schema") == "projectbuddy/ubuntu-vagrant-clone@1", "unexpected schema")
    require(receipt.get("outcome") == "PASS" and receipt.get("failures") == [], "receipt outcome is not PASS")
    require(receipt.get("sourceBox") == {
        "receiptSha256": digest(arguments.box_receipt),
        "boxSha256": box_receipt.get("box", {}).get("sha256"),
    }, "source box identity mismatch")
    require(receipt.get("clone") == {
        "vm": {"name": arguments.vm_name, "uuid": expected_vm_uuid},
        "bootId": boot_id, "finalState": "poweroff",
    }, "clone identity, boot, or final state mismatch")
    require(receipt.get("evidenceSha256") == expected_hashes, "clone evidence hash mismatch")
    require(probe.get("host", {}).get("bootId") == boot_id, "clone captures do not share one boot ID")
    require(
        probe.get("inputs") == [20, 22] and probe.get("expectedOutput") == 42
        and probe.get("exitCode") == 0
        and bool(re.search(r"Operation output:\s*42\s+RunVI operation succeeded\.", probe.get("output", ""))),
        "raw known-answer capture failed",
    )
    require(
        runtime.get("os") == {"distribution": "ubuntu", "version": "24.04", "codename": "noble"}
        and runtime.get("automaticLoginEnabled") is False
        and runtime.get("labviewPackageVersion") == "26.3.1.49165-0+f13",
        "raw runtime capture is invalid",
    )
    require(
        about.get("version") == "26.3.2" and str(about.get("build_number")) == "4056"
        and about.get("edition") == "Professional" and about.get("valid_through") == "Perpetual"
        and about.get("valid_activation_code") is True,
        "raw VIPM activation capture is invalid",
    )
    targets = labview.get("labview_versions", [])
    require(
        len(targets) == 1 and targets[0].get("version_year") == "2026"
        and targets[0].get("bitness") == 64 and targets[0].get("vi_server_tcp_port") == 3363,
        "raw LabVIEW target capture is invalid",
    )
    packages = [
        item for item in installed.get("dependencies", [])
        if item.get("name") == "vi_technologies_lib_super_network_streams"
    ]
    require(
        len(packages) == 1 and packages[0].get("installed_version") == "2.0.0.21"
        and packages[0].get("status") == "installed",
        "raw installed-package capture is invalid",
    )
    runtime_contract = receipt.get("runtime", {})
    require(runtime_contract.get("os") == {"distribution": "ubuntu", "version": "24.04", "codename": "noble"}, "unexpected guest OS")
    require(runtime_contract.get("automaticLoginEnabled") is False, "automatic login remains enabled")
    require(runtime_contract.get("labviewPackageVersion") == "26.3.1.49165-0+f13", "unexpected LabVIEW package")
    require(runtime_contract.get("knownAnswer") == {"inputs": [20, 22], "actualOutput": 42, "viServerPort": 3363}, "known-answer or VI Server contract mismatch")
    require(runtime_contract.get("vipm") == {"version": "26.3.2", "buildNumber": "4056", "edition": "Professional", "validThrough": "Perpetual"}, "VIPM contract mismatch")
    require(runtime_contract.get("package") == {"name": "vi_technologies_lib_super_network_streams", "version": "2.0.0.21", "status": "installed"}, "installed package contract mismatch")
    forbidden = sorted(FORBIDDEN_FIELDS.intersection(keys(receipt)))
    require(not forbidden, f"forbidden fields present: {', '.join(forbidden)}")
    require(all(re.fullmatch(r"[a-f0-9]{64}", value) for value in expected_hashes.values()), "evidence SHA-256 is invalid")

    if errors:
        for error in errors:
            print(f"Vagrant clone receipt: FAIL: {error}", file=sys.stderr)
        return 2
    print(f"Vagrant clone receipt: PASS: {arguments.vm_name} ({expected_vm_uuid})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())