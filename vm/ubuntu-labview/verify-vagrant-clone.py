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
import time
import uuid


PACKAGE_NAME = "vi_technologies_lib_super_network_streams"
PACKAGE_VERSION = "2.0.0.21"
SOURCE_FILES = (
    "activation-known-answer-capture.json",
    "sandbox-runtime-state.json",
    "sandbox-vipm-about.json",
    "sandbox-vipm-labview.json",
    "sandbox-vipm-installed.json",
)


def fail(message: str, code: int = 2) -> int:
    print(f"Vagrant clone verification: FAIL: {message}", file=sys.stderr)
    return code


def digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require_internal_nvme(path: pathlib.Path) -> None:
    mount = run(["findmnt", "-n", "-o", "SOURCE", "-T", str(path)])
    devices = {line.strip() for line in mount.stdout.splitlines() if line.strip().startswith("/dev/")}
    if mount.returncode != 0 or len(devices) != 1:
        raise ValueError("cannot resolve clone evidence block storage")
    transport = run(["lsblk", "-n", "-o", "TRAN", devices.pop()])
    if transport.returncode != 0 or transport.stdout.strip().lower() != "nvme":
        raise ValueError("clone evidence is not backed by internal NVMe")


def parse_vm_info(output: str) -> dict[str, str]:
    values = {}
    for line in output.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value.strip().strip('"')
    return values


def run(
    command: list[str], environment: dict[str, str] | None = None, stdin=None, timeout: int = 60,
) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            command, check=False, capture_output=True, text=True,
            env=environment, stdin=stdin, start_new_session=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(command, 124, "", "operation timed out")


def write_atomic(path: pathlib.Path, value: dict) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except Exception:
        pathlib.Path(temporary).unlink(missing_ok=True)
        raise


def main() -> int:
    script_dir = pathlib.Path(__file__).resolve().parent
    parser = argparse.ArgumentParser()
    parser.add_argument("--vm-name", required=True)
    parser.add_argument("--vm-uuid", required=True)
    parser.add_argument("--vagrant-machine-id-file", type=pathlib.Path, required=True)
    parser.add_argument("--ssh-port", type=int, required=True)
    parser.add_argument("--guest-password-file", type=pathlib.Path, required=True)
    parser.add_argument("--box", type=pathlib.Path, required=True)
    parser.add_argument("--manifest", type=pathlib.Path, required=True)
    parser.add_argument("--base-receipt", type=pathlib.Path, required=True)
    parser.add_argument("--public-receipt", type=pathlib.Path, required=True)
    parser.add_argument("--public-appliance-receipt", type=pathlib.Path, required=True)
    parser.add_argument("--activation-receipt", type=pathlib.Path, required=True)
    parser.add_argument("--package-report", type=pathlib.Path, required=True)
    parser.add_argument("--package-artifact", type=pathlib.Path, required=True)
    parser.add_argument("--embedded-vagrantfile", type=pathlib.Path, required=True)
    parser.add_argument("--box-receipt", type=pathlib.Path, required=True)
    parser.add_argument("--evidence-dir", type=pathlib.Path, required=True)
    parser.add_argument("--vboxmanage")
    parser.add_argument("--run", action="store_true")
    arguments = parser.parse_args()

    try:
        expected_vm_uuid = str(uuid.UUID(arguments.vm_uuid))
        if not re.fullmatch(r"[A-Za-z0-9._-]+", arguments.vm_name):
            raise ValueError("VM name is invalid")
        if not 1024 <= arguments.ssh_port <= 65535:
            raise ValueError("SSH port must be from 1024 through 65535")
        if not arguments.guest_password_file.is_absolute() or not arguments.guest_password_file.is_file():
            raise ValueError("guest password file must be an existing absolute path")
        if arguments.guest_password_file.stat().st_mode & 0o077:
            raise ValueError("guest password file must have mode 600 or stricter")
        if not arguments.evidence_dir.is_absolute() or not arguments.evidence_dir.is_dir():
            raise ValueError("evidence directory must be an existing absolute path")
        if arguments.evidence_dir.stat().st_mode & 0o077:
            raise ValueError("evidence directory must have mode 700 or stricter")
        require_internal_nvme(arguments.evidence_dir)
        if not arguments.vagrant_machine_id_file.is_absolute() or not arguments.vagrant_machine_id_file.is_file():
            raise ValueError("Vagrant machine ID file must be an existing absolute path")
        vagrant_vm_uuid = str(uuid.UUID(arguments.vagrant_machine_id_file.read_text(encoding="utf-8").strip()))
        if vagrant_vm_uuid != expected_vm_uuid:
            raise ValueError("Vagrant machine ID does not match the expected clone UUID")
        base_receipt = json.loads(arguments.base_receipt.read_text(encoding="utf-8"))
        box_receipt = json.loads(arguments.box_receipt.read_text(encoding="utf-8"))
        expected_fingerprint = base_receipt["sshHostKey"]["fingerprint"]
        if not re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", expected_fingerprint):
            raise ValueError("base receipt SSH fingerprint is invalid")
        if box_receipt.get("schema") != "projectbuddy/ubuntu-vagrant-box@1" or box_receipt.get("outcome") != "PASS":
            raise ValueError("box receipt is not a passing governed receipt")
        source_vm_uuid = str(uuid.UUID(box_receipt.get("source", {}).get("vm", {}).get("uuid", "")))
        if expected_vm_uuid == source_vm_uuid:
            raise ValueError("clean clone UUID must differ from the source appliance UUID")
        if digest(arguments.base_receipt) != box_receipt.get("provenanceSha256", {}).get("baseReceiptSha256"):
            raise ValueError("base receipt does not match box provenance")
        activated_snapshot_uuid = str(uuid.UUID(box_receipt["source"]["activatedSnapshot"]["uuid"]))
        package_snapshot_uuid = str(uuid.UUID(box_receipt["source"]["snapshot"]["uuid"]))
    except (KeyError, OSError, ValueError, json.JSONDecodeError) as error:
        return fail(str(error))

    receipt_path = arguments.evidence_dir / "vagrant-clone-receipt.json"
    receipt_hash_path = arguments.evidence_dir / "vagrant-clone-receipt.json.sha256"
    if receipt_path.exists() or receipt_hash_path.exists() or any((arguments.evidence_dir / name).exists() for name in SOURCE_FILES):
        return fail("clone evidence output already exists")

    vboxmanage = arguments.vboxmanage or shutil.which("VBoxManage")
    if not vboxmanage:
        return fail("VBoxManage is missing", 127)
    box_verification = run([
        str(script_dir / "verify-vagrant-box-receipt.py"), str(arguments.box_receipt),
        "--box", str(arguments.box), "--manifest", str(arguments.manifest),
        "--base-receipt", str(arguments.base_receipt), "--public-receipt", str(arguments.public_receipt),
        "--public-appliance-receipt", str(arguments.public_appliance_receipt),
        "--activation-receipt", str(arguments.activation_receipt),
        "--package-report", str(arguments.package_report), "--package-artifact", str(arguments.package_artifact),
        "--embedded-vagrantfile", str(arguments.embedded_vagrantfile),
        "--vm-name", box_receipt["source"]["vm"]["name"], "--vm-uuid", source_vm_uuid,
        "--activated-snapshot-uuid", activated_snapshot_uuid,
        "--package-snapshot-uuid", package_snapshot_uuid,
    ], timeout=900)
    if box_verification.returncode != 0:
        return fail("independent box receipt verification failed")
    vm_result = run([vboxmanage, "showvminfo", arguments.vm_name, "--machinereadable"])
    vm_info = parse_vm_info(vm_result.stdout)
    if vm_result.returncode != 0:
        return fail("cannot read clone VM identity")
    if vm_info.get("UUID") != expected_vm_uuid:
        return fail("clone VM UUID does not match expected identity")
    if vm_info.get("VMState") != "running":
        return fail("clone VM must be running")
    forwarding_rules = [value for key, value in vm_info.items() if key.startswith("Forwarding(")]
    guest_ssh_rules = [rule for rule in forwarding_rules if len(rule.split(",")) == 6 and rule.split(",")[5] == "22"]
    expected_forwarding = [
        rule for rule in forwarding_rules
        if len(rule.split(",")) == 6
        and rule.split(",")[1:] == ["tcp", "127.0.0.1", str(arguments.ssh_port), "", "22"]
    ]
    if len(expected_forwarding) != 1 or guest_ssh_rules != expected_forwarding:
        return fail("clone does not have exactly one loopback SSH forwarding rule for the expected port")

    if not arguments.run:
        print("Vagrant clone verification: PLAN")
        print(f"clone_vm_uuid={expected_vm_uuid}")
        print(f"ssh_port={arguments.ssh_port}")
        print(f"source_box_sha256={box_receipt.get('box', {}).get('sha256', '')}")
        return 0

    private_root = pathlib.Path(tempfile.mkdtemp(prefix="sandbox-vagrant-clone-", dir=os.environ.get("XDG_RUNTIME_DIR")))
    private_root.chmod(0o700)
    known_hosts = private_root / "known-hosts"
    askpass = private_root / "askpass.sh"
    askpass.write_text(f"#!/bin/sh\ncat {str(arguments.guest_password_file)!r}\n", encoding="utf-8")
    askpass.chmod(0o700)
    environment = os.environ.copy()
    environment.update({"DISPLAY": ":0", "SSH_ASKPASS": str(askpass), "SSH_ASKPASS_REQUIRE": "force"})
    ssh = [
        "ssh", "-p", str(arguments.ssh_port), "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={known_hosts}", "-o", "ConnectTimeout=10", "actor@127.0.0.1",
    ]
    scp = [
        "scp", "-P", str(arguments.ssh_port), "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={known_hosts}", "-o", "ConnectTimeout=10",
    ]
    powered_off = False
    try:
        enrolled = False
        for _ in range(30):
            known_hosts.unlink(missing_ok=True)
            enrollment = run([
                str(script_dir / "enroll-ssh-host-key.py"),
                "--host", "127.0.0.1", "--port", str(arguments.ssh_port),
                "--expected-fingerprint", expected_fingerprint, "--out", str(known_hosts),
            ], timeout=20)
            if enrollment.returncode == 0:
                enrolled = True
                break
            time.sleep(5)
        if not enrolled:
            raise RuntimeError("clone SSH identity did not become ready")

        prepare = run([*ssh, "rm -rf /home/actor/sandbox-clone-proof; install -d -m 700 /home/actor/sandbox-clone-proof"], environment, timeout=90)
        if prepare.returncode != 0:
            raise RuntimeError("cannot prepare clone proof directory")
        upload = run([*scp, str(script_dir / "capture-activation.sh"), "actor@127.0.0.1:/home/actor/sandbox-clone-proof/capture-activation.sh"], environment, timeout=120)
        if upload.returncode != 0:
            raise RuntimeError("cannot upload clone capture script")
        capture = run([
            *ssh,
            "chmod 700 /home/actor/sandbox-clone-proof/capture-activation.sh && "
            "/home/actor/sandbox-clone-proof/capture-activation.sh --output-dir /home/actor/sandbox-clone-proof/evidence",
        ], environment, timeout=600)
        if capture.returncode != 0:
            raise RuntimeError("clone runtime capture failed")
        for name in SOURCE_FILES:
            copied = run([
                *scp, f"actor@127.0.0.1:/home/actor/sandbox-clone-proof/evidence/{name}",
                str(arguments.evidence_dir / name),
            ], environment, timeout=120)
            if copied.returncode != 0:
                raise RuntimeError(f"cannot retrieve clone evidence: {name}")
            (arguments.evidence_dir / name).chmod(0o600)

        probe = json.loads((arguments.evidence_dir / SOURCE_FILES[0]).read_text(encoding="utf-8"))
        runtime = json.loads((arguments.evidence_dir / SOURCE_FILES[1]).read_text(encoding="utf-8"))
        about = json.loads((arguments.evidence_dir / SOURCE_FILES[2]).read_text(encoding="utf-8"))
        labview = json.loads((arguments.evidence_dir / SOURCE_FILES[3]).read_text(encoding="utf-8"))
        installed = json.loads((arguments.evidence_dir / SOURCE_FILES[4]).read_text(encoding="utf-8"))
        output = probe.get("output", "")
        if probe.get("exitCode") != 0 or not re.search(r"Operation output:\s*42\s+RunVI operation succeeded\.", output):
            raise RuntimeError("clone LabVIEW known-answer probe failed")
        if runtime.get("os") != {"distribution": "ubuntu", "version": "24.04", "codename": "noble"}:
            raise RuntimeError("clone guest OS does not match the appliance contract")
        if runtime.get("labviewPackageVersion") != "26.3.1.49165-0+f13" or runtime.get("automaticLoginEnabled") is not False:
            raise RuntimeError("clone LabVIEW package or security state is invalid")
        if not (
            about.get("version") == "26.3.2" and str(about.get("build_number")) == "4056"
            and about.get("edition") == "Professional" and about.get("valid_through") == "Perpetual"
            and about.get("valid_activation_code") is True
        ):
            raise RuntimeError("clone VIPM activation state is invalid")
        targets = labview.get("labview_versions", [])
        if len(targets) != 1 or targets[0].get("version_year") != "2026" or targets[0].get("bitness") != 64 or targets[0].get("vi_server_tcp_port") != 3363:
            raise RuntimeError("clone LabVIEW target or VI Server contract is invalid")
        packages = [item for item in installed.get("dependencies", []) if item.get("name") == PACKAGE_NAME]
        if len(packages) != 1 or packages[0].get("installed_version") != PACKAGE_VERSION or packages[0].get("status") != "installed":
            raise RuntimeError("clone sample package is not installed at the expected version")
        boot_id = str(uuid.UUID(runtime.get("bootId", "")))
        if probe.get("host", {}).get("bootId") != boot_id:
            raise RuntimeError("clone captures do not share one boot ID")

        receipt = {
            "schema": "projectbuddy/ubuntu-vagrant-clone@1",
            "sourceBox": {
                "receiptSha256": digest(arguments.box_receipt),
                "boxSha256": box_receipt["box"]["sha256"],
            },
            "clone": {"vm": {"name": arguments.vm_name, "uuid": expected_vm_uuid}, "bootId": boot_id},
            "evidenceSha256": {pathlib.Path(name).stem: digest(arguments.evidence_dir / name) for name in SOURCE_FILES},
            "runtime": {
                "os": runtime["os"], "automaticLoginEnabled": False,
                "labviewPackageVersion": runtime["labviewPackageVersion"],
                "knownAnswer": {"inputs": [20, 22], "actualOutput": 42, "viServerPort": 3363},
                "vipm": {"version": "26.3.2", "buildNumber": "4056", "edition": "Professional", "validThrough": "Perpetual"},
                "package": {"name": PACKAGE_NAME, "version": PACKAGE_VERSION, "status": "installed"},
            },
            "failures": [],
            "outcome": "PASS",
        }
        cleanup = run([*ssh, "rm -rf /home/actor/sandbox-clone-proof"], environment, timeout=90)
        if cleanup.returncode != 0:
            raise RuntimeError("clone guest evidence cleanup failed")
        with arguments.guest_password_file.open("r", encoding="utf-8") as password_stream:
            run([*ssh, 'sudo -S -p "" systemctl poweroff'], environment, password_stream, timeout=30)
        for _ in range(60):
            state = run([vboxmanage, "showvminfo", expected_vm_uuid, "--machinereadable"])
            if parse_vm_info(state.stdout).get("VMState") == "poweroff":
                powered_off = True
                break
            time.sleep(2)
        if not powered_off:
            run([vboxmanage, "controlvm", expected_vm_uuid, "acpipowerbutton"])
            for _ in range(60):
                state = run([vboxmanage, "showvminfo", expected_vm_uuid, "--machinereadable"])
                if parse_vm_info(state.stdout).get("VMState") == "poweroff":
                    powered_off = True
                    break
                time.sleep(2)
        if not powered_off:
            raise RuntimeError("clone did not power off cleanly")
        receipt["clone"]["finalState"] = "poweroff"
        write_atomic(receipt_path, receipt)
        verification = run([
            str(script_dir / "verify-vagrant-clone-receipt.py"), str(receipt_path),
            "--box-receipt", str(arguments.box_receipt), "--evidence-dir", str(arguments.evidence_dir),
            "--vm-name", arguments.vm_name, "--vm-uuid", expected_vm_uuid,
        ])
        if verification.returncode != 0:
            raise RuntimeError("independent clone receipt verification failed")
        receipt_hash_path.write_text(f"{digest(receipt_path)}  {receipt_path.name}\n", encoding="utf-8")
        receipt_hash_path.chmod(0o600)
    except (KeyError, OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        receipt_path.unlink(missing_ok=True)
        receipt_hash_path.unlink(missing_ok=True)
        return fail(str(error), 1)
    finally:
        if not powered_off:
            run([vboxmanage, "controlvm", expected_vm_uuid, "acpipowerbutton"])
            for _ in range(60):
                state = run([vboxmanage, "showvminfo", expected_vm_uuid, "--machinereadable"])
                if parse_vm_info(state.stdout).get("VMState") == "poweroff":
                    powered_off = True
                    break
                time.sleep(2)
            if not powered_off:
                print("Vagrant clone verification: WARN: clone did not power off during cleanup", file=sys.stderr)
        shutil.rmtree(private_root, ignore_errors=True)

    print(f"Vagrant clone verification: PASS: {receipt_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())