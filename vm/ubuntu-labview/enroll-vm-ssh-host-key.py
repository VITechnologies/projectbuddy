#!/usr/bin/env python3
import argparse
import os
import pathlib
import re
import shutil
import subprocess
import sys
import uuid


FINGERPRINT_PROPERTY = "/ProjectBuddy/SshHostEd25519Fingerprint"
FINGERPRINT_PATTERN = re.compile(r"SHA256:[A-Za-z0-9+/]{20,}={0,2}")
VM_NAME_PATTERN = re.compile(r"[A-Za-z0-9._-]+")


def fail(message: str) -> int:
    print(f"VM SSH host-key enrollment: FAIL: {message}", file=sys.stderr)
    return 2


def run_vbox(vboxmanage: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [vboxmanage, *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vm-name", required=True)
    parser.add_argument("--vm-uuid", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2222)
    parser.add_argument("--out", type=pathlib.Path, required=True)
    parser.add_argument("--vboxmanage")
    arguments = parser.parse_args()

    if not VM_NAME_PATTERN.fullmatch(arguments.vm_name):
        return fail("VM name is invalid")
    try:
        expected_uuid = str(uuid.UUID(arguments.vm_uuid))
    except ValueError:
        return fail("VM UUID is invalid")
    vboxmanage = arguments.vboxmanage or shutil.which("VBoxManage") or shutil.which("VBoxManage.exe")
    if not vboxmanage:
        return fail("VBoxManage is missing")

    try:
        vm_info = run_vbox(vboxmanage, "showvminfo", arguments.vm_name, "--machinereadable")
    except OSError as error:
        return fail(f"cannot run VBoxManage: {error}")
    if vm_info.returncode != 0:
        return fail("cannot read VM identity")
    uuid_matches = re.findall(r'^UUID="([^"]+)"$', vm_info.stdout, re.MULTILINE)
    if len(uuid_matches) != 1:
        return fail("VM UUID is missing or ambiguous")
    try:
        actual_uuid = str(uuid.UUID(uuid_matches[0]))
    except ValueError:
        return fail("registered VM UUID is invalid")
    if actual_uuid != expected_uuid:
        return fail("VM UUID does not match expected identity")

    property_result = run_vbox(
        vboxmanage,
        "guestproperty", "get", arguments.vm_name, FINGERPRINT_PROPERTY,
    )
    if property_result.returncode != 0:
        return fail("cannot read guest SSH fingerprint property")
    property_matches = re.findall(r"^Value: (\S+)$", property_result.stdout, re.MULTILINE)
    if len(property_matches) != 1 or not FINGERPRINT_PATTERN.fullmatch(property_matches[0]):
        return fail("guest SSH fingerprint property is missing or invalid")

    if os.name == "nt":
        powershell = shutil.which("pwsh")
        if not powershell:
            return fail("PowerShell is missing")
        enrollment_command = [
            powershell,
            "-NoProfile",
            "-File", str(pathlib.Path(__file__).with_name("Enroll-SshHostKey.ps1")),
            "-ExpectedFingerprint", property_matches[0],
            "-KnownHostsPath", str(arguments.out),
            "-SshHost", arguments.host,
            "-SshPort", str(arguments.port),
            "-PythonExecutable", sys.executable,
        ]
    else:
        enrollment_command = [
            sys.executable,
            str(pathlib.Path(__file__).with_name("enroll-ssh-host-key.py")),
            "--host", arguments.host,
            "--port", str(arguments.port),
            "--expected-fingerprint", property_matches[0],
            "--out", str(arguments.out),
        ]
    enrollment = subprocess.run(
        enrollment_command,
        check=False,
        capture_output=True,
        text=True,
    )
    if enrollment.returncode != 0:
        return fail("collected SSH key does not match the VM guest property")

    print(f"VM SSH host-key enrollment: PASS: {arguments.vm_name} ({expected_uuid})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())