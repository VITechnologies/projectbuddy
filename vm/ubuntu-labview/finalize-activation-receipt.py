#!/usr/bin/env python3
import argparse
import hashlib
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import uuid


NAME_PATTERN = re.compile(r"[A-Za-z0-9._-]+")
ACTIVATED_SNAPSHOT_NAME = "labview2026-q3-pro-activated-ready"
SOURCE_FILES = {
    "--capture": "activation-known-answer-capture.json",
    "--runtime-state": "sandbox-runtime-state.json",
    "--vipm-about": "sandbox-vipm-about.json",
    "--vipm-labview": "sandbox-vipm-labview.json",
    "--vipm-installed": "sandbox-vipm-installed.json",
}


def fail(message: str, code: int = 2) -> int:
    print(f"activation receipt finalizer: FAIL: {message}", file=sys.stderr)
    return code


def parse_vm_info(output: str) -> dict[str, str]:
    values = {}
    for line in output.splitlines():
        match = re.fullmatch(r'([^=]+)="(.*)"', line)
        if match:
            values[match.group(1)] = match.group(2)
    return values


def write_atomic(path: pathlib.Path, content: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = pathlib.Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    script_dir = pathlib.Path(__file__).resolve().parent
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-dir", type=pathlib.Path, required=True)
    parser.add_argument("--vm-name", required=True)
    parser.add_argument("--vm-uuid", required=True)
    parser.add_argument("--snapshot-name", default=ACTIVATED_SNAPSHOT_NAME)
    parser.add_argument("--snapshot-uuid", required=True)
    parser.add_argument("--vboxmanage")
    arguments = parser.parse_args()

    if not arguments.evidence_dir.is_absolute():
        return fail("evidence directory must be absolute")
    if not NAME_PATTERN.fullmatch(arguments.vm_name):
        return fail("VM name is invalid")
    if arguments.snapshot_name != ACTIVATED_SNAPSHOT_NAME:
        return fail("snapshot name does not match the activation receipt contract")
    try:
        expected_vm_uuid = str(uuid.UUID(arguments.vm_uuid))
        expected_snapshot_uuid = str(uuid.UUID(arguments.snapshot_uuid))
    except ValueError:
        return fail("VM or snapshot UUID is invalid")
    if not arguments.evidence_dir.is_dir():
        return fail("evidence directory is missing")
    powershell = None
    if os.name == "nt":
        powershell = shutil.which("pwsh")
        if not powershell:
            return fail("PowerShell is missing", 127)
        directory_acl = subprocess.run([
            powershell, "-NoProfile", "-File", str(script_dir / "Test-PrivateDirectoryAcl.ps1"),
            "-Path", str(arguments.evidence_dir),
        ], check=False, capture_output=True, text=True)
        if directory_acl.returncode != 0:
            return fail("evidence directory ACL is not private")
    elif arguments.evidence_dir.stat().st_mode & 0o077:
        return fail("evidence directory must have mode 700")

    receipt = arguments.evidence_dir / "activation-receipt.json"
    receipt_hash = arguments.evidence_dir / "activation-receipt.json.sha256"
    if receipt.exists() or receipt_hash.exists():
        return fail("activation receipt output already exists")

    vboxmanage = arguments.vboxmanage or shutil.which("VBoxManage") or shutil.which("VBoxManage.exe")
    if not vboxmanage:
        return fail("VBoxManage is missing", 127)
    vm_result = subprocess.run(
        [vboxmanage, "showvminfo", arguments.vm_name, "--machinereadable"],
        check=False,
        capture_output=True,
        text=True,
    )
    if vm_result.returncode != 0:
        return fail("cannot read VM identity")
    vm_info = parse_vm_info(vm_result.stdout)
    if vm_info.get("VMState") == "livesnapshotting":
        return fail("snapshot serialization is still active")
    try:
        actual_vm_uuid = str(uuid.UUID(vm_info.get("UUID", "")))
        actual_snapshot_uuid = str(uuid.UUID(vm_info.get("CurrentSnapshotUUID", "")))
    except ValueError:
        return fail("registered VM or current snapshot UUID is invalid")
    if actual_vm_uuid != expected_vm_uuid:
        return fail("VM UUID does not match expected identity")
    if vm_info.get("CurrentSnapshotName") != arguments.snapshot_name:
        return fail("activated-ready snapshot is not current")
    if actual_snapshot_uuid != expected_snapshot_uuid:
        return fail("current snapshot UUID does not match expected identity")

    builder_arguments = []
    for option, filename in SOURCE_FILES.items():
        source = arguments.evidence_dir / filename
        if not source.is_file():
            return fail(f"required evidence is missing: {filename}")
        if os.name == "nt":
            source_acl = subprocess.run([
                powershell, "-NoProfile", "-File", str(script_dir / "Test-PrivateFileAcl.ps1"),
                "-Path", str(source), "-AllowInheritance",
            ], check=False, capture_output=True, text=True)
            if source_acl.returncode != 0:
                return fail(f"evidence file ACL is not private: {filename}")
        elif source.stat().st_mode & 0o077:
            return fail(f"evidence file must have mode 600: {filename}")
        builder_arguments.extend((option, str(source)))

    build = subprocess.run([
        sys.executable,
        str(script_dir / "build-activation-receipt.py"),
        *builder_arguments,
        "--vm-name", arguments.vm_name,
        "--vm-uuid", expected_vm_uuid,
        "--snapshot-name", arguments.snapshot_name,
        "--snapshot-uuid", expected_snapshot_uuid,
        "--out", str(receipt),
    ], check=False)
    if build.returncode != 0:
        return fail("activation receipt build failed", build.returncode)

    try:
        digest = hashlib.sha256(receipt.read_bytes()).hexdigest()
        write_atomic(receipt_hash, f"{digest}  {receipt.name}\n")
        if os.name != "nt":
            receipt_hash.chmod(0o600)
            if receipt.stat().st_mode & 0o077 or receipt_hash.stat().st_mode & 0o077:
                raise OSError("receipt outputs do not enforce mode 600")
        else:
            for output in (receipt, receipt_hash):
                output_acl = subprocess.run([
                    powershell, "-NoProfile", "-File", str(script_dir / "Test-PrivateFileAcl.ps1"),
                    "-Path", str(output), "-AllowInheritance",
                ], check=False, capture_output=True, text=True)
                if output_acl.returncode != 0:
                    raise OSError("receipt output ACL is not private")
        expected_line = f"{hashlib.sha256(receipt.read_bytes()).hexdigest()}  {receipt.name}\n"
        if receipt_hash.read_text(encoding="ascii") != expected_line:
            raise OSError("receipt hash verification failed")
    except OSError as error:
        receipt.unlink(missing_ok=True)
        receipt_hash.unlink(missing_ok=True)
        return fail(str(error), 1)

    print(f"activation receipt finalizer: PASS: {receipt}")
    print(f"activation receipt finalizer: PASS: snapshot {arguments.snapshot_name} ({expected_snapshot_uuid})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())