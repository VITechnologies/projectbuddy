#!/usr/bin/env python3
import argparse
import json
import pathlib
import re
import sys
import uuid


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("receipt", type=pathlib.Path)
    parser.add_argument("--vm-name", required=True)
    parser.add_argument("--vm-uuid", required=True)
    parser.add_argument("--ssh-host-fingerprint", required=True)
    arguments = parser.parse_args()

    try:
        expected_uuid = str(uuid.UUID(arguments.vm_uuid))
    except ValueError:
        print("base receipt: FAIL: expected VM UUID is invalid", file=sys.stderr)
        return 2
    if not re.fullmatch(r"SHA256:[A-Za-z0-9+/]{20,}={0,2}", arguments.ssh_host_fingerprint):
        print("base receipt: FAIL: expected SSH host fingerprint is invalid", file=sys.stderr)
        return 2

    try:
        receipt = json.loads(arguments.receipt.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"base receipt: FAIL: {error}", file=sys.stderr)
        return 2

    errors = []

    def require(condition: bool, message: str) -> None:
        if not condition:
            errors.append(message)

    require(receipt.get("schema") == "projectbuddy/ubuntu-base-bootstrap@2", "unexpected schema")
    require(receipt.get("os") == {"distribution": "ubuntu", "version": "24.04", "codename": "noble"}, "unexpected guest OS")
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

    ssh_host_key = receipt.get("sshHostKey", {})
    require(ssh_host_key.get("algorithm") == "ssh-ed25519", "unexpected SSH host-key algorithm")
    require(
        ssh_host_key.get("fingerprint") == arguments.ssh_host_fingerprint,
        "SSH host fingerprint does not match expected identity",
    )

    tools = receipt.get("tools", {})
    for name in ("sshd", "virtualBoxGuestService"):
        tool = tools.get(name, {})
        require(isinstance(tool.get("path"), str) and tool["path"].startswith("/"), f"{name} path is missing")
        require(isinstance(tool.get("version"), str) and bool(tool["version"]), f"{name} version is missing")

    services = receipt.get("services", {})
    for name in ("ssh", "virtualBoxGuestUtils"):
        service = services.get(name, {})
        require(service.get("activeState") == "active", f"{name} is not active")
        require(service.get("enabledState") == "enabled", f"{name} is not enabled")

    if errors:
        for error in errors:
            print(f"base receipt: FAIL: {error}", file=sys.stderr)
        return 2
    print(f"base receipt: PASS: {vm['name']} ({vm['uuid']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())