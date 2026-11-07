#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid


FINGERPRINT_PROPERTY = "/ProjectBuddy/SshHostEd25519Fingerprint"
FINGERPRINT_PATTERN = re.compile(r"SHA256:[A-Za-z0-9+/]{20,}={0,2}")
NAME_PATTERN = re.compile(r"[A-Za-z0-9._-]+")
REMOTE_DIRECTORY = "/tmp/sandbox-public-provisioning"
PREACTIVATION_SNAPSHOT_NAME = "labview2026-q3-pro-installed-preactivation"


def fail(message: str, code: int = 2) -> int:
    print(f"public provisioning adapter: FAIL: {message}", file=sys.stderr)
    return code


def run_capture(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=False, capture_output=True, text=True)


def parse_vm_info(output: str) -> dict[str, str]:
    values = {}
    for line in output.splitlines():
        key, separator, raw_value = line.partition("=")
        if not separator:
            continue
        if len(raw_value) >= 2 and raw_value.startswith('"') and raw_value.endswith('"'):
            raw_value = raw_value[1:-1]
        values[key] = raw_value
    return values


def print_vbox_progress(vboxmanage: str, vm_name: str, phase: str, started: float, readiness: str) -> None:
    vm_result = run_capture([vboxmanage, "showvminfo", vm_name, "--machinereadable"])
    vm_info = parse_vm_info(vm_result.stdout) if vm_result.returncode == 0 else {}
    elapsed = int(time.monotonic() - started)
    print(
        "public provisioning progress: "
        f"phase={phase} elapsed_seconds={elapsed} "
        f"vm_state={vm_info.get('VMState', 'unavailable')} "
        f"guest_additions_run_level={vm_info.get('GuestAdditionsRunLevel', 'unavailable')} "
        f"guest_additions_version={vm_info.get('GuestAdditionsVersion', 'unavailable')} "
        f"readiness={readiness}",
        flush=True,
    )


def write_atomic(path: pathlib.Path, content: str, encoding: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = pathlib.Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding=encoding) as handle:
            handle.write(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def guest_command(
    vboxmanage: str,
    vm_name: str,
    operation: str,
    guest_user: str,
    password_file: pathlib.Path,
    *arguments: str,
) -> list[str]:
    return [
        vboxmanage,
        "guestcontrol",
        vm_name,
        operation,
        f"--username={guest_user}",
        f"--passwordfile={password_file}",
        *arguments,
    ]


def print_plan(snapshot_name: str) -> None:
    steps = [
        "verify VM identity and running state",
        "enroll VM-bound SSH host key",
        "guestcontrol --passwordfile=<guest-password-file> copyfrom base receipt",
        "copy and verify base receipt",
        "guestcontrol --passwordfile=<guest-password-file> copyto public provisioning inputs",
        "run provision-guest.sh over VM-bound SSH with sudo password on stdin",
        "guestcontrol --passwordfile=<guest-password-file> copyfrom public receipt",
        "copy and verify public provisioning receipt",
        "cleanly power off guest",
        f"take snapshot {snapshot_name}",
        "write redacted public appliance checkpoint and SHA-256 files",
    ]
    for index, step in enumerate(steps, start=1):
        print(f"{index}. {step}")


def main() -> int:
    script_dir = pathlib.Path(__file__).resolve().parent
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--run", action="store_true")
    parser.add_argument("--vm-name", required=True)
    parser.add_argument("--vm-uuid", required=True)
    parser.add_argument("--guest-user", default="actor")
    parser.add_argument("--guest-password-file", type=pathlib.Path, required=True)
    parser.add_argument("--evidence-dir", type=pathlib.Path, required=True)
    parser.add_argument("--known-hosts", type=pathlib.Path, required=True)
    parser.add_argument("--manifest", type=pathlib.Path, default=script_dir / "provisioning-manifest.json")
    parser.add_argument("--snapshot-name", default=PREACTIVATION_SNAPSHOT_NAME)
    parser.add_argument("--ssh-host", default="127.0.0.1")
    parser.add_argument("--ssh-port", type=int, default=2222)
    parser.add_argument("--wait-seconds", type=int, default=900)
    parser.add_argument("--vboxmanage")
    parser.add_argument("--resume-base-receipt", action="store_true")
    arguments = parser.parse_args()

    if not NAME_PATTERN.fullmatch(arguments.vm_name):
        return fail("VM name is invalid")
    if arguments.snapshot_name != PREACTIVATION_SNAPSHOT_NAME:
        return fail("snapshot name does not match the public appliance contract")
    if not re.fullmatch(r"[a-z_][a-z0-9_-]*", arguments.guest_user):
        return fail("guest user is invalid")
    try:
        expected_uuid = str(uuid.UUID(arguments.vm_uuid))
    except ValueError:
        return fail("VM UUID is invalid")
    if not 1024 <= arguments.ssh_port <= 65535:
        return fail("SSH port must be from 1024 through 65535")
    if arguments.wait_seconds < 1:
        return fail("wait seconds must be positive")
    for label, path in (
        ("guest password file", arguments.guest_password_file),
        ("evidence directory", arguments.evidence_dir),
        ("known-hosts path", arguments.known_hosts),
    ):
        if not path.is_absolute():
            return fail(f"{label} must be an absolute path")

    if not arguments.run:
        print_plan(arguments.snapshot_name)
        return 0

    vboxmanage = arguments.vboxmanage or shutil.which("VBoxManage") or shutil.which("VBoxManage.exe")
    if not vboxmanage:
        return fail("VBoxManage is missing", 127)
    ssh_keygen = shutil.which("ssh-keygen")
    if not ssh_keygen:
        return fail("ssh-keygen is missing", 127)
    ssh = shutil.which("ssh")
    if not ssh:
        return fail("ssh is missing", 127)
    required_files = [
        arguments.guest_password_file,
        arguments.manifest,
        script_dir / "provision-guest.sh",
        script_dir / "ni-signing-authority.asc",
    ]
    if any(not path.is_file() for path in required_files):
        return fail("a required password or provisioning input file is missing")
    if os.name == "nt":
        powershell = shutil.which("pwsh")
        if not powershell:
            return fail("PowerShell is missing", 127)
        password_acl = subprocess.run([
            powershell, "-NoProfile", "-File", str(script_dir / "Test-PrivateFileAcl.ps1"),
            "-Path", str(arguments.guest_password_file),
        ], check=False, capture_output=True, text=True)
        if password_acl.returncode != 0:
            return fail("guest password file ACL is not private")
    elif arguments.guest_password_file.stat().st_mode & 0o077:
        return fail("guest password file must have mode 600 or stricter")
    if arguments.known_hosts.exists():
        return fail("known-hosts output already exists")
    base_receipt = arguments.evidence_dir / "base-bootstrap-receipt.json"
    public_receipt = arguments.evidence_dir / "public-provisioning-receipt.json"
    checkpoint_path = arguments.evidence_dir / "public-appliance-receipt.json"
    hash_path = checkpoint_path.with_suffix(checkpoint_path.suffix + ".sha256")
    later_evidence = (public_receipt, checkpoint_path, hash_path)
    if any(path.exists() for path in later_evidence) or (base_receipt.exists() and not arguments.resume_base_receipt):
        return fail("evidence output already exists; verify identity before replacing it")

    vm_result = run_capture([vboxmanage, "showvminfo", arguments.vm_name, "--machinereadable"])
    if vm_result.returncode != 0:
        return fail("cannot read VM identity")
    vm_info = parse_vm_info(vm_result.stdout)
    try:
        actual_uuid = str(uuid.UUID(vm_info.get("UUID", "")))
    except ValueError:
        return fail("registered VM UUID is invalid")
    if actual_uuid != expected_uuid:
        return fail("VM UUID does not match expected identity")
    if vm_info.get("VMState") != "running":
        return fail("VM must be running")
    snapshots = run_capture([vboxmanage, "snapshot", arguments.vm_name, "list", "--machinereadable"])
    no_snapshots = "\n".join((snapshots.stdout, snapshots.stderr)).strip() == "This machine does not have any snapshots"
    if snapshots.returncode != 0 and not no_snapshots:
        return fail("cannot inspect existing snapshots")
    for line in snapshots.stdout.splitlines():
        snapshot_match = re.fullmatch(r'SnapshotName(?:-\d+)?="(.*)"', line)
        if snapshot_match and snapshot_match.group(1) == arguments.snapshot_name:
            return fail("target pre-activation snapshot already exists")

    arguments.evidence_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        evidence_acl = subprocess.run([
            powershell, "-NoProfile", "-File", str(script_dir / "Initialize-PrivateDirectory.ps1"),
            "-Path", str(arguments.evidence_dir),
        ], check=False, capture_output=True, text=True)
        if evidence_acl.returncode != 0:
            return fail("evidence directory ACL is not private")
    else:
        arguments.evidence_dir.chmod(0o700)
        if arguments.evidence_dir.stat().st_mode & 0o077:
            return fail("evidence directory must enforce mode 700")

    def secure_evidence_file(path: pathlib.Path) -> bool:
        if os.name == "nt":
            acl_result = subprocess.run([
                powershell, "-NoProfile", "-File", str(script_dir / "Test-PrivateFileAcl.ps1"),
                "-Path", str(path), "-AllowInheritance",
            ], check=False, capture_output=True, text=True)
            protected = acl_result.returncode == 0
        else:
            try:
                path.chmod(0o600)
                protected = not path.stat().st_mode & 0o077
            except OSError:
                protected = False
        if not protected:
            path.unlink(missing_ok=True)
        return protected

    wait_started = time.monotonic()
    deadline = wait_started + arguments.wait_seconds
    next_progress = wait_started
    fingerprint = ""
    while time.monotonic() < deadline:
        property_result = run_capture([
            vboxmanage,
            "guestproperty", "get", arguments.vm_name, FINGERPRINT_PROPERTY,
        ])
        match = re.fullmatch(r"Value: (\S+)\s*", property_result.stdout)
        if property_result.returncode == 0 and match and FINGERPRINT_PATTERN.fullmatch(match.group(1)):
            fingerprint = match.group(1)
            break
        now = time.monotonic()
        if now >= next_progress:
            print_vbox_progress(vboxmanage, arguments.vm_name, "bootstrap-wait", wait_started, "pending")
            next_progress = now + 30
        time.sleep(2)
    if not fingerprint:
        return fail("guest SSH fingerprint property did not become ready")
    print_vbox_progress(vboxmanage, arguments.vm_name, "bootstrap-wait", wait_started, "ready")

    print("public provisioning progress: phase=ssh-enrollment status=started", flush=True)
    enrollment = subprocess.run([
        sys.executable,
        str(script_dir / "enroll-vm-ssh-host-key.py"),
        "--vm-name", arguments.vm_name,
        "--vm-uuid", expected_uuid,
        "--host", arguments.ssh_host,
        "--port", str(arguments.ssh_port),
        "--out", str(arguments.known_hosts),
        "--vboxmanage", vboxmanage,
    ], check=False)
    if enrollment.returncode != 0:
        return fail("VM-bound SSH host-key enrollment failed")
    print("public provisioning progress: phase=ssh-enrollment status=complete", flush=True)

    remote_base_receipt = "/var/lib/projectbuddy/base-bootstrap-receipt.json"
    remote_public_receipt = "/var/lib/projectbuddy/public-provisioning-receipt.json"

    if not base_receipt.exists():
        copy_base = subprocess.run(guest_command(
            vboxmanage, arguments.vm_name, "copyfrom", arguments.guest_user,
            arguments.guest_password_file, remote_base_receipt, str(base_receipt),
        ), check=False)
        if copy_base.returncode != 0:
            return fail("cannot retrieve base receipt")
    if not secure_evidence_file(base_receipt):
        return fail("base receipt permissions are not private")
    fingerprint_result = run_capture([ssh_keygen, "-lf", str(arguments.known_hosts), "-E", "sha256"])
    fingerprints = FINGERPRINT_PATTERN.findall(fingerprint_result.stdout)
    if fingerprint_result.returncode != 0 or fingerprints != [fingerprint]:
        return fail("enrolled known-hosts fingerprint is inconsistent")
    verify_base = subprocess.run([
        sys.executable, str(script_dir / "verify-base-receipt.py"), str(base_receipt),
        "--vm-name", arguments.vm_name,
        "--vm-uuid", expected_uuid,
        "--ssh-host-fingerprint", fingerprint,
    ], check=False)
    if verify_base.returncode != 0:
        return fail("base receipt verification failed")
    print("public provisioning progress: phase=base-receipt status=verified", flush=True)

    mkdir_result = subprocess.run(guest_command(
        vboxmanage, arguments.vm_name, "run", arguments.guest_user,
        arguments.guest_password_file, "--exe=/usr/bin/install", "--",
        "-d", "-m", "0700", REMOTE_DIRECTORY,
    ), check=False)
    if mkdir_result.returncode != 0:
        return fail("cannot create guest provisioning directory")
    inputs = [arguments.manifest, script_dir / "provision-guest.sh", script_dir / "ni-signing-authority.asc"]
    guest_script = script_dir / "provision-guest.sh"
    original_guest_script = guest_script.read_bytes()
    try:
        guest_script.write_bytes(original_guest_script.replace(b"\r\n", b"\n"))
        for source in inputs:
            copy_input = subprocess.run(guest_command(
                vboxmanage, arguments.vm_name, "copyto", arguments.guest_user,
                arguments.guest_password_file, str(source), f"{REMOTE_DIRECTORY}/{source.name}",
            ), check=False)
            if copy_input.returncode != 0:
                return fail("cannot upload public provisioning inputs")
    finally:
        guest_script.write_bytes(original_guest_script)

    password = arguments.guest_password_file.read_text(encoding="utf-8").rstrip("\r\n")
    if not password:
        return fail("guest password file is empty")
    askpass_directory = tempfile.TemporaryDirectory(prefix="sandbox-ssh-askpass-")
    askpass_root = pathlib.Path(askpass_directory.name)
    if os.name == "nt":
        askpass = askpass_root / "askpass.cmd"
        askpass.write_text(f'@type "{arguments.guest_password_file}"\r\n', encoding="utf-8")
    else:
        askpass = askpass_root / "askpass.sh"
        askpass.write_text(
            f"#!/bin/sh\ncat {shlex.quote(str(arguments.guest_password_file))}\n",
            encoding="utf-8",
        )
        askpass.chmod(0o700)
    ssh_environment = os.environ.copy()
    ssh_environment.update({
        "DISPLAY": ssh_environment.get("DISPLAY", ":0"),
        "SSH_ASKPASS": str(askpass),
        "SSH_ASKPASS_REQUIRE": "force",
    })

    def run_ssh_sudo(command: list[str]) -> subprocess.CompletedProcess[str]:
        remote_command = shlex.join(["sudo", "-S", "--prompt=", *command])
        return subprocess.run([
            ssh, "-p", str(arguments.ssh_port),
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={arguments.known_hosts}",
            f"{arguments.guest_user}@{arguments.ssh_host}", remote_command,
        ], input=password + "\n", text=True, check=False, env=ssh_environment)

    print("public provisioning progress: phase=guest-provisioning status=started", flush=True)
    provision = run_ssh_sudo([
        "/bin/bash", f"{REMOTE_DIRECTORY}/provision-guest.sh",
        "--manifest", f"{REMOTE_DIRECTORY}/{arguments.manifest.name}",
        "--primary-user", arguments.guest_user,
    ])
    if provision.returncode != 0:
        return fail("guest public provisioning failed", 1)
    print("public provisioning progress: phase=guest-provisioning status=complete", flush=True)

    copy_public = subprocess.run(guest_command(
        vboxmanage, arguments.vm_name, "copyfrom", arguments.guest_user,
        arguments.guest_password_file, remote_public_receipt, str(public_receipt),
    ), check=False)
    if copy_public.returncode != 0:
        return fail("cannot retrieve public provisioning receipt")
    if not secure_evidence_file(public_receipt):
        return fail("public provisioning receipt permissions are not private")
    verify_public = subprocess.run([
        sys.executable, str(script_dir / "verify-public-provisioning-receipt.py"),
        str(public_receipt), "--manifest", str(arguments.manifest),
        "--vm-name", arguments.vm_name, "--vm-uuid", expected_uuid,
    ], check=False)
    if verify_public.returncode != 0:
        return fail("public provisioning receipt verification failed")
    print("public provisioning progress: phase=public-receipt status=verified", flush=True)

    cleanup = subprocess.run(guest_command(
        vboxmanage, arguments.vm_name, "run", arguments.guest_user,
        arguments.guest_password_file, "--exe=/bin/rm", "--", "-rf", REMOTE_DIRECTORY,
    ), check=False)
    if cleanup.returncode != 0:
        return fail("cannot remove guest provisioning inputs", 1)
    shutdown = run_ssh_sudo(["/usr/bin/systemctl", "poweroff"])
    if shutdown.returncode != 0:
        return fail("guest shutdown request failed", 1)

    shutdown_started = time.monotonic()
    deadline = shutdown_started + arguments.wait_seconds
    next_progress = shutdown_started
    while time.monotonic() < deadline:
        state_result = run_capture([vboxmanage, "showvminfo", arguments.vm_name, "--machinereadable"])
        if parse_vm_info(state_result.stdout).get("VMState") == "poweroff":
            break
        now = time.monotonic()
        if now >= next_progress:
            print_vbox_progress(vboxmanage, arguments.vm_name, "shutdown-wait", shutdown_started, "pending")
            next_progress = now + 30
        time.sleep(2)
    else:
        return fail("guest did not power off cleanly", 1)
    print_vbox_progress(vboxmanage, arguments.vm_name, "shutdown-wait", shutdown_started, "ready")

    print("public provisioning progress: phase=preactivation-snapshot status=started", flush=True)
    snapshot = subprocess.run([
        vboxmanage, "snapshot", arguments.vm_name, "take", arguments.snapshot_name,
        "--description", "Public Ubuntu 24.04, LabVIEW 2026 Q3 Pro, and VIPM 26.3.2 provisioning verified; activation pending.",
    ], check=False)
    if snapshot.returncode != 0:
        return fail("pre-activation snapshot failed", 1)
    print("public provisioning progress: phase=preactivation-snapshot status=complete", flush=True)
    final_info = run_capture([vboxmanage, "showvminfo", arguments.vm_name, "--machinereadable"])
    if final_info.returncode != 0:
        return fail("cannot read pre-activation snapshot identity", 1)
    final_vm_info = parse_vm_info(final_info.stdout)
    try:
        final_vm_uuid = str(uuid.UUID(final_vm_info.get("UUID", "")))
        snapshot_uuid = str(uuid.UUID(final_vm_info.get("CurrentSnapshotUUID", "")))
    except ValueError:
        return fail("post-snapshot VM or snapshot UUID is invalid", 1)
    if final_vm_uuid != expected_uuid:
        return fail("post-snapshot VM UUID does not match expected identity", 1)
    if final_vm_info.get("VMState") != "poweroff":
        return fail("VM is not powered off after pre-activation snapshot", 1)
    if final_vm_info.get("CurrentSnapshotName") != arguments.snapshot_name:
        return fail("pre-activation snapshot is not current", 1)

    def digest(path: pathlib.Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    checkpoint = {
        "schema": "projectbuddy/ubuntu-public-appliance@1",
        "vm": {"name": arguments.vm_name, "uuid": expected_uuid},
        "snapshot": {"name": arguments.snapshot_name, "uuid": snapshot_uuid},
        "sources": {
            "baseReceiptSha256": digest(base_receipt),
            "manifestSha256": digest(arguments.manifest),
            "publicProvisioningReceiptSha256": digest(public_receipt),
        },
        "outcome": "PASS",
    }
    write_atomic(checkpoint_path, json.dumps(checkpoint, indent=2, sort_keys=True) + "\n", "utf-8")
    if not secure_evidence_file(checkpoint_path):
        return fail("public appliance checkpoint permissions are not private", 1)
    verify_checkpoint = subprocess.run([
        sys.executable, str(script_dir / "verify-public-appliance-receipt.py"), str(checkpoint_path),
        "--base-receipt", str(base_receipt),
        "--public-receipt", str(public_receipt),
        "--manifest", str(arguments.manifest),
        "--vm-name", arguments.vm_name,
        "--vm-uuid", expected_uuid,
        "--snapshot-name", arguments.snapshot_name,
        "--snapshot-uuid", snapshot_uuid,
    ], check=False)
    if verify_checkpoint.returncode != 0:
        checkpoint_path.unlink(missing_ok=True)
        return fail("public appliance receipt verification failed", 1)
    checkpoint_hash = digest(checkpoint_path)
    write_atomic(hash_path, f"{checkpoint_hash}  {checkpoint_path.name}\n", "ascii")
    if not secure_evidence_file(hash_path):
        checkpoint_path.unlink(missing_ok=True)
        return fail("public appliance hash permissions are not private", 1)
    print(f"public provisioning adapter: PASS: snapshot {arguments.snapshot_name} ({snapshot_uuid})")
    print(f"public provisioning adapter: PASS: receipt {checkpoint_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())