#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import uuid


ACTIVATED_SNAPSHOT_NAME = "labview2026-q3-pro-activated-ready"
VM_NAME_PATTERN = re.compile(r"[A-Za-z0-9._-]+")


def read_json(path: pathlib.Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_atomic(path: pathlib.Path, content: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = pathlib.Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    script_dir = pathlib.Path(__file__).resolve().parent
    parser = argparse.ArgumentParser()
    parser.add_argument("--capture", type=pathlib.Path, required=True)
    parser.add_argument("--runtime-state", type=pathlib.Path, required=True)
    parser.add_argument("--vipm-about", type=pathlib.Path, required=True)
    parser.add_argument("--vipm-labview", type=pathlib.Path, required=True)
    parser.add_argument("--vipm-installed", type=pathlib.Path, required=True)
    parser.add_argument("--vm-name", required=True)
    parser.add_argument("--vm-uuid", required=True)
    parser.add_argument("--snapshot-name", default=ACTIVATED_SNAPSHOT_NAME)
    parser.add_argument("--snapshot-uuid", required=True)
    parser.add_argument("--out", type=pathlib.Path, required=True)
    arguments = parser.parse_args()

    if not VM_NAME_PATTERN.fullmatch(arguments.vm_name):
        print("activation receipt builder: FAIL: VM name is invalid", file=sys.stderr)
        return 2
    if arguments.snapshot_name != ACTIVATED_SNAPSHOT_NAME:
        print("activation receipt builder: FAIL: snapshot name does not match the receipt contract", file=sys.stderr)
        return 2
    try:
        arguments.vm_uuid = str(uuid.UUID(arguments.vm_uuid))
        arguments.snapshot_uuid = str(uuid.UUID(arguments.snapshot_uuid))
    except ValueError:
        print("activation receipt builder: FAIL: VM or snapshot UUID is invalid", file=sys.stderr)
        return 2
    if arguments.out.exists():
        print("activation receipt builder: FAIL: output already exists", file=sys.stderr)
        return 2
    powershell = None
    if os.name == "nt":
        if not arguments.out.parent.is_dir():
            print("activation receipt builder: FAIL: output directory is missing", file=sys.stderr)
            return 2
        powershell = shutil.which("pwsh")
        if not powershell:
            print("activation receipt builder: FAIL: PowerShell is missing", file=sys.stderr)
            return 127
        directory_acl = subprocess.run([
            powershell, "-NoProfile", "-File", str(script_dir / "Test-PrivateDirectoryAcl.ps1"),
            "-Path", str(arguments.out.parent),
        ], check=False, capture_output=True, text=True)
        if directory_acl.returncode != 0:
            print("activation receipt builder: FAIL: output directory ACL is not private", file=sys.stderr)
            return 2
    else:
        arguments.out.parent.mkdir(parents=True, exist_ok=True)

    sources = {
        "knownAnswerCapture": arguments.capture,
        "runtimeState": arguments.runtime_state,
        "vipmAbout": arguments.vipm_about,
        "vipmLabview": arguments.vipm_labview,
        "vipmInstalled": arguments.vipm_installed,
    }
    try:
        capture = read_json(arguments.capture)
        runtime = read_json(arguments.runtime_state)
        vipm_about = read_json(arguments.vipm_about)
        vipm_labview = read_json(arguments.vipm_labview)
        vipm_installed = read_json(arguments.vipm_installed)
        source_hashes = {name: sha256(path) for name, path in sources.items()}
    except (OSError, ValueError) as error:
        print(f"activation receipt builder: FAIL: {error}", file=sys.stderr)
        return 2

    capture_boot = capture.get("host", {}).get("bootId")
    runtime_boot = runtime.get("bootId")
    if not capture_boot or capture_boot != runtime_boot:
        print("activation receipt builder: FAIL: captures do not share one guest boot ID", file=sys.stderr)
        return 2

    targets = vipm_labview.get("labview_versions")
    if not isinstance(targets, list) or len(targets) != 1:
        print("activation receipt builder: FAIL: VIPM must discover exactly one LabVIEW target", file=sys.stderr)
        return 2
    target = targets[0]

    output = capture.get("output", "")
    answer = re.search(r"Operation output:\s*(-?\d+)\s+RunVI operation succeeded\.", output)
    actual_output = int(answer.group(1)) if answer else None
    receipt = {
        "schema": "projectbuddy/ubuntu-activation@1",
        "evidenceSha256": source_hashes,
        "os": runtime.get("os"),
        "vm": {
            "name": arguments.vm_name,
            "uuid": arguments.vm_uuid,
            "bootId": runtime_boot,
            "snapshot": {"name": arguments.snapshot_name, "uuid": arguments.snapshot_uuid},
        },
        "security": {"automaticLoginEnabled": runtime.get("automaticLoginEnabled")},
        "labview": {
            "edition": "Professional",
            "package": "ni-labview-2026-pro",
            "packageVersion": runtime.get("labviewPackageVersion"),
            "appPath": target.get("app_path"),
            "viServerPort": target.get("vi_server_tcp_port"),
            "knownAnswerProbe": {
                "inputs": capture.get("inputs"),
                "expectedOutput": capture.get("expectedOutput"),
                "actualOutput": actual_output,
                "exitCode": capture.get("exitCode"),
                "wallMs": capture.get("wallMs"),
                "displayMode": capture.get("displayMode"),
                "output": output,
            },
        },
        "vipm": {
            "version": vipm_about.get("version"),
            "buildNumber": vipm_about.get("build_number"),
            "edition": vipm_about.get("edition"),
            "validThrough": vipm_about.get("valid_through"),
            "validActivationCode": vipm_about.get("valid_activation_code"),
            "labviewTarget": {
                "version": target.get("version_year"),
                "bitness": target.get("bitness"),
                "appPath": target.get("app_path"),
                "viServerPort": target.get("vi_server_tcp_port"),
            },
            "installedLookupAvailable": vipm_installed.get("installed_lookup", {}).get("available"),
        },
        "failures": [],
        "outcome": "PASS",
    }

    write_atomic(arguments.out, json.dumps(receipt, indent=2) + "\n")
    if os.name == "nt":
        output_acl = subprocess.run([
            powershell, "-NoProfile", "-File", str(script_dir / "Test-PrivateFileAcl.ps1"),
            "-Path", str(arguments.out), "-AllowInheritance",
        ], check=False, capture_output=True, text=True)
        if output_acl.returncode != 0:
            arguments.out.unlink(missing_ok=True)
            print("activation receipt builder: FAIL: output ACL is not private", file=sys.stderr)
            return 2
    else:
        arguments.out.chmod(0o600)
        if arguments.out.stat().st_mode & 0o077:
            arguments.out.unlink(missing_ok=True)
            print("activation receipt builder: FAIL: output must enforce mode 600", file=sys.stderr)
            return 2
    verifier = script_dir / "verify-activation-receipt.py"
    result = subprocess.run(
        [
            str(verifier), str(arguments.out),
            "--vm-name", arguments.vm_name,
            "--vm-uuid", arguments.vm_uuid,
            "--boot-id", runtime_boot,
            "--snapshot-uuid", arguments.snapshot_uuid,
        ],
        check=False,
    )
    if result.returncode != 0:
        arguments.out.unlink(missing_ok=True)
        return result.returncode
    print(f"activation receipt builder: PASS: {arguments.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())