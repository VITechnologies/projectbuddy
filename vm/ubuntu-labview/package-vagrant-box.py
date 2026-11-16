#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))

from lane_box.common import LaneBoxError  # noqa: E402
from vagrant_clone import core, guest  # noqa: E402

ACTIVATED_SNAPSHOT_NAME = "labview2026-q3-pro-activated-ready"
PACKAGE_SNAPSHOT_NAME = "labview2026-q3-pro-package-ready"
NAME_PATTERN = re.compile(r"[A-Za-z0-9._-]+")
FINGERPRINT = re.compile(r"SHA256:[A-Za-z0-9+/]{43}")
SESSION_PREFIX = "projectbuddy-package-scan-"
ACTIVATION_CAPTURES = {
    "knownAnswerCapture": "activation-known-answer-capture.json",
    "runtimeState": "sandbox-runtime-state.json",
    "vipmAbout": "sandbox-vipm-about.json",
    "vipmLabview": "sandbox-vipm-labview.json",
    "vipmInstalled": "sandbox-vipm-installed.json",
}


def fail(message: str, code: int = 2) -> int:
    print(f"Vagrant box packaging: FAIL: {message}", file=sys.stderr)
    return code


def digest(path: pathlib.Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def parse_vm_info(output: str) -> dict[str, str]:
    values = {}
    for line in output.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip().strip('"')
    return values


def verify_hash_file(path: pathlib.Path, target: pathlib.Path) -> None:
    fields = path.read_text(encoding="utf-8").strip().split()
    if len(fields) != 2 or fields[1].lstrip("*") != target.name:
        raise ValueError(f"invalid hash file: {path.name}")
    if fields[0] != digest(target):
        raise ValueError(f"hash mismatch: {target.name}")


def require_private_directory(path: pathlib.Path) -> None:
    if not path.is_absolute() or not path.is_dir():
        raise ValueError(f"directory must be an existing absolute path: {path}")
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise ValueError(f"directory must have mode 700 or stricter: {path}")


def require_private_storage(path: pathlib.Path) -> None:
    require_private_directory(path)
    mount = subprocess.run(
        ["findmnt", "-n", "-o", "SOURCE,FSTYPE,OPTIONS", "-T", str(path)],
        check=False, capture_output=True, text=True,
    )
    devices = []
    for line in mount.stdout.splitlines():
        fields = line.split(maxsplit=2)
        if len(fields) == 3 and fields[0].startswith("/dev/"):
            devices.append(fields)
    if mount.returncode != 0 or len(devices) != 1:
        raise ValueError(f"cannot resolve private storage for {path}")
    _, filesystem, options = devices[0]
    option_set = set(options.split(","))
    if filesystem.startswith("fuse") or "allow_other" in option_set:
        raise ValueError(f"private storage cannot use FUSE or allow_other: {path}")

    probe = pathlib.Path(tempfile.mkdtemp(prefix=".custody-probe-", dir=path))
    probe_file = probe / "private-file"
    try:
        probe.chmod(0o700)
        descriptor = os.open(probe_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        probe_file.chmod(0o600)
        if probe.stat().st_mode & 0o777 != 0o700 or probe_file.stat().st_mode & 0o777 != 0o600:
            raise ValueError(f"filesystem does not enforce private modes: {path}")
    finally:
        probe_file.unlink(missing_ok=True)
        probe.rmdir()


def require_internal_nvme(path: pathlib.Path) -> str:
    mount = subprocess.run(
        ["findmnt", "-n", "-o", "SOURCE", "-T", str(path)],
        check=False, capture_output=True, text=True,
    )
    devices = {line.strip() for line in mount.stdout.splitlines() if line.strip().startswith("/dev/")}
    if mount.returncode != 0 or len(devices) != 1:
        raise ValueError(f"cannot resolve block storage for {path}")
    source = devices.pop()
    transport = subprocess.run(
        ["lsblk", "-n", "-o", "TRAN", source],
        check=False, capture_output=True, text=True,
    )
    if transport.returncode != 0 or transport.stdout.strip().lower() != "nvme":
        raise ValueError(f"storage is not internal NVMe: {path}")
    return source


def run_checked(
    command: list[str], message: str, env: dict[str, str] | None = None, timeout: int = 900,
) -> subprocess.CompletedProcess:
    process = subprocess.Popen(command, env=env, start_new_session=True)
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        raise RuntimeError(f"{message}: timed out")
    if returncode != 0:
        raise RuntimeError(message)
    return subprocess.CompletedProcess(command, returncode)


def write_atomic(path: pathlib.Path, content: str) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
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
    parser.add_argument("--activated-snapshot-uuid", required=True)
    parser.add_argument("--package-snapshot-uuid")
    parser.add_argument("--evidence-dir", type=pathlib.Path, required=True)
    parser.add_argument("--package-report", type=pathlib.Path, required=True)
    parser.add_argument("--package-artifact", type=pathlib.Path, required=True)
    parser.add_argument("--vagrant-home", type=pathlib.Path, required=True)
    parser.add_argument("--temp-dir", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--receipt-out", type=pathlib.Path, required=True)
    parser.add_argument("--ssh-port", type=int, required=True)
    parser.add_argument("--guest-password-file", type=pathlib.Path, required=True)
    parser.add_argument("--vboxmanage")
    parser.add_argument("--vagrant")
    parser.add_argument("--run", action="store_true")
    arguments = parser.parse_args()
    receipt_hash_out = pathlib.Path(f"{arguments.receipt_out}.sha256")
    if os.name != "nt":
        os.umask(0o077)

    if not NAME_PATTERN.fullmatch(arguments.vm_name):
        return fail("VM name is invalid")
    try:
        expected_vm_uuid = str(uuid.UUID(arguments.vm_uuid))
        activated_snapshot_uuid = str(uuid.UUID(arguments.activated_snapshot_uuid))
        package_snapshot_uuid = str(uuid.UUID(arguments.package_snapshot_uuid)) if arguments.package_snapshot_uuid else None
        if arguments.ssh_port not in core.PORT_RANGE:
            raise ValueError("SSH port must be from 1024 through 65535")
        password_file = arguments.guest_password_file
        if not password_file.is_absolute() or not password_file.is_file() or password_file.stat().st_mode & 0o077:
            raise ValueError("guest password file must be an existing absolute path with mode 600 or stricter")
        require_private_storage(arguments.evidence_dir)
        require_private_storage(arguments.vagrant_home)
        require_private_storage(arguments.vagrant_home / "boxes")
        require_private_storage(arguments.temp_dir)
        if not arguments.output.is_absolute() or not arguments.receipt_out.is_absolute():
            raise ValueError("output paths must be absolute")
        require_private_storage(arguments.output.parent)
        require_private_storage(arguments.receipt_out.parent)
        if arguments.output.exists() or arguments.receipt_out.exists() or receipt_hash_out.exists():
            raise ValueError("box, receipt, or receipt hash output already exists")
        output_device = require_internal_nvme(arguments.output.parent)
        require_internal_nvme(arguments.evidence_dir)
        require_internal_nvme(arguments.receipt_out.parent)
        require_internal_nvme(arguments.package_report.parent)
        require_internal_nvme(arguments.package_artifact.parent)
        require_private_storage(arguments.package_report.parent)
        require_private_storage(arguments.package_artifact.parent)
        for package_input in (arguments.package_report, arguments.package_artifact):
            if not package_input.is_file() or package_input.stat().st_mode & 0o077:
                raise ValueError(f"package evidence must exist with mode 600 or stricter: {package_input}")
        if require_internal_nvme(arguments.vagrant_home / "boxes") != output_device:
            raise ValueError("Vagrant box store and box output must use the same NVMe filesystem")
        if require_internal_nvme(arguments.temp_dir) != output_device:
            raise ValueError("temporary extraction and box output must use the same NVMe filesystem")
    except (OSError, ValueError) as error:
        return fail(str(error))

    manifest = script_dir / "provisioning-manifest.json"
    embedded_vagrantfile = script_dir / "Vagrantfile.box"
    files = {
        "baseReceiptSha256": arguments.evidence_dir / "base-bootstrap-receipt.json",
        "publicProvisioningReceiptSha256": arguments.evidence_dir / "public-provisioning-receipt.json",
        "publicApplianceReceiptSha256": arguments.evidence_dir / "public-appliance-receipt.json",
        "activationReceiptSha256": arguments.evidence_dir / "activation-receipt.json",
        "packageBuildReportSha256": arguments.package_report,
        "packageArtifactSha256": arguments.package_artifact,
        "manifestSha256": manifest,
        "embeddedVagrantfileSha256": embedded_vagrantfile,
    }
    try:
        for path in files.values():
            if not path.is_file():
                raise ValueError(f"required input is missing: {path.name}")
        base = json.loads(files["baseReceiptSha256"].read_text(encoding="utf-8"))
        host_key = base.get("sshHostKey") if isinstance(base, dict) else None
        fingerprint = host_key.get("fingerprint") if isinstance(host_key, dict) else None
        if not isinstance(fingerprint, str) or not FINGERPRINT.fullmatch(fingerprint):
            raise ValueError("base receipt SSH fingerprint is invalid")
        activation = json.loads(files["activationReceiptSha256"].read_text(encoding="utf-8"))
        public_appliance = json.loads(files["publicApplianceReceiptSha256"].read_text(encoding="utf-8"))
        manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
        package_report = json.loads(arguments.package_report.read_text(encoding="utf-8"))
        verify_hash_file(arguments.evidence_dir / "activation-receipt.json.sha256", files["activationReceiptSha256"])
        verify_hash_file(arguments.evidence_dir / "public-appliance-receipt.json.sha256", files["publicApplianceReceiptSha256"])
        if any(path.stat().st_mode & 0o077 for path in files.values() if path.parent == arguments.evidence_dir):
            raise ValueError("evidence inputs must have mode 600 or stricter")
        boot_id = str(uuid.UUID(activation.get("vm", {}).get("bootId", "")))
        for key, filename in ACTIVATION_CAPTURES.items():
            capture = arguments.evidence_dir / filename
            if not capture.is_file() or digest(capture) != activation.get("evidenceSha256", {}).get(key):
                raise ValueError(f"activation capture hash mismatch: {filename}")
        if package_report.get("schema") != "projectbuddy/linux-vip-package-proof@1" or package_report.get("outcome") != "PASS":
            raise ValueError("package-build report is not a passing governed receipt")
        if package_report.get("vm") != {
            "name": arguments.vm_name, "uuid": expected_vm_uuid,
            "baseSnapshotName": ACTIVATED_SNAPSHOT_NAME, "baseSnapshotUuid": activated_snapshot_uuid,
        }:
            raise ValueError("package-build report VM or base snapshot identity mismatch")
        package = package_report.get("package", {})
        if package.get("sha256") != digest(arguments.package_artifact) or package.get("file") != arguments.package_artifact.name:
            raise ValueError("package artifact does not match package-build report")
        if (package.get("name"), package.get("version"), package.get("status")) != (
            "vi_technologies_lib_super_network_streams", "2.0.0.21", "verified-installed",
        ):
            raise ValueError("package-build report does not match the appliance contract")
        package_marker = (
            f"projectbuddy/package-ready@1;activated={activated_snapshot_uuid};"
            f"report={digest(arguments.package_report)};artifact={digest(arguments.package_artifact)}"
        )
        public_snapshot = public_appliance.get("snapshot", {})
        public_snapshot_uuid = str(uuid.UUID(public_snapshot.get("uuid", "")))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return fail(str(error))

    try:
        run_checked([
            str(script_dir / "verify-activation-receipt.py"), str(files["activationReceiptSha256"]),
            "--vm-name", arguments.vm_name, "--vm-uuid", expected_vm_uuid,
            "--boot-id", boot_id, "--snapshot-uuid", activated_snapshot_uuid,
        ], "activation receipt verification failed")
        run_checked([
            str(script_dir / "verify-public-appliance-receipt.py"), str(files["publicApplianceReceiptSha256"]),
            "--base-receipt", str(files["baseReceiptSha256"]),
            "--public-receipt", str(files["publicProvisioningReceiptSha256"]),
            "--manifest", str(manifest), "--vm-name", arguments.vm_name,
            "--vm-uuid", expected_vm_uuid, "--snapshot-name", str(public_snapshot.get("name", "")),
            "--snapshot-uuid", public_snapshot_uuid,
        ], "public appliance receipt verification failed")
    except RuntimeError as error:
        return fail(str(error))

    vboxmanage = arguments.vboxmanage or shutil.which("VBoxManage")
    vagrant = arguments.vagrant or shutil.which("vagrant")
    if not vboxmanage or not vagrant:
        return fail("VBoxManage or Vagrant is missing", 127)
    vm_result = subprocess.run(
        [vboxmanage, "showvminfo", arguments.vm_name, "--machinereadable"],
        check=False, capture_output=True, text=True,
    )
    vm_info = parse_vm_info(vm_result.stdout)
    if vm_result.returncode != 0:
        return fail("cannot read VM identity")
    if vm_info.get("UUID") != expected_vm_uuid:
        return fail("registered VM UUID does not match expected identity")
    if vm_info.get("VMState") != "running":
        return fail("source VM must be running for the runner scan")
    try:
        core.check_ssh_forward(vm_info, arguments.ssh_port, "source VM")
    except LaneBoxError as error:
        return fail(str(error))
    current_snapshot_name = vm_info.get("CurrentSnapshotName")
    current_snapshot_uuid = vm_info.get("CurrentSnapshotUUID")
    if current_snapshot_name == ACTIVATED_SNAPSHOT_NAME:
        if current_snapshot_uuid != activated_snapshot_uuid or package_snapshot_uuid is not None:
            return fail("activated-ready snapshot identity does not match packaging inputs")
    elif current_snapshot_name == PACKAGE_SNAPSHOT_NAME:
        if package_snapshot_uuid is None or current_snapshot_uuid != package_snapshot_uuid:
            return fail("package-ready snapshot identity does not match packaging inputs")
        snapshots = subprocess.run(
            [vboxmanage, "snapshot", expected_vm_uuid, "list", "--machinereadable"],
            check=False, capture_output=True, text=True,
        )
        snapshot_values = parse_vm_info(snapshots.stdout)
        marker_matches = []
        for key, name in snapshot_values.items():
            match = re.fullmatch(r"SnapshotName((?:-\d+)*)", key)
            if match and name == PACKAGE_SNAPSHOT_NAME:
                suffix = match.group(1)
                marker_matches.append(
                    snapshot_values.get(f"SnapshotUUID{suffix}") == package_snapshot_uuid
                    and snapshot_values.get(f"SnapshotDescription{suffix}") == package_marker
                )
        if snapshots.returncode != 0 or marker_matches != [True]:
            return fail("package-ready snapshot does not carry the expected activated ancestry marker")
    else:
        return fail("source VM is not at the activated-ready or package-ready boundary")

    command = [
        vagrant, "package", "--base", expected_vm_uuid,
        "--output", str(arguments.output), "--vagrantfile", str(embedded_vagrantfile),
    ]
    # REQ-105: scan the boot that gets packaged. The packager starts nothing in the guest between the scan and its
    # own power-off; the scan cannot see what a process already running in the guest writes before shutdown ends.
    target = guest.Guest(uuid=expected_vm_uuid, ssh_port=arguments.ssh_port, fingerprint=fingerprint,
                         password_file=arguments.guest_password_file, vboxmanage=vboxmanage)
    try:
        session_root = pathlib.Path(tempfile.mkdtemp(prefix=SESSION_PREFIX, dir=os.environ.get("XDG_RUNTIME_DIR")))
    except OSError as error:
        return fail(f"cannot create the guest session directory: {error}")
    try:
        try:
            session = guest.Session(target, session_root, guest.run_command)
            guest.enroll(target, session, guest.run_command, time.sleep)
            runner_scan = guest.scan_runner(session)
        except (LaneBoxError, OSError) as error:
            return fail(f"runner scan failed: {error}")
        findings = runner_scan["runnerUnits"] + runner_scan["runnerConfigurations"]
        if findings:
            return fail("REQ-105: the source VM carries an Actions runner: " + ", ".join(findings))
        if not arguments.run:
            print("Vagrant box packaging: PLAN")
            print("command=" + " ".join(command))
            print(f"source_vm_uuid={expected_vm_uuid}")
            print(f"activated_snapshot_uuid={activated_snapshot_uuid}")
            print(f"package_snapshot={'create' if package_snapshot_uuid is None else package_snapshot_uuid}")
            print(f"runner_scan=clean boot_id={runner_scan['bootId']}")
            return 0
        try:
            powered_off = guest.power_off(target, session, guest.run_command, time.sleep)
        except OSError:
            powered_off = False
    finally:
        shutil.rmtree(session_root, ignore_errors=True)
    if not powered_off:
        return fail("source VM did not power off after the runner scan", 1)
    stopped = subprocess.run(
        [vboxmanage, "showvminfo", expected_vm_uuid, "--machinereadable"],
        check=False, capture_output=True, text=True,
    )
    stopped_info = parse_vm_info(stopped.stdout)
    if stopped.returncode != 0 or stopped_info.get("UUID") != expected_vm_uuid or (
        stopped_info.get("VMState"), stopped_info.get("CurrentSnapshotName"), stopped_info.get("CurrentSnapshotUUID"),
    ) != ("poweroff", current_snapshot_name, current_snapshot_uuid):
        return fail("source VM is not powered off at the scanned snapshot", 1)

    environment = os.environ.copy()
    environment.update({"VAGRANT_HOME": str(arguments.vagrant_home), "TMPDIR": str(arguments.temp_dir)})
    try:
        if package_snapshot_uuid is None:
            run_checked([
                vboxmanage, "snapshot", expected_vm_uuid, "take", PACKAGE_SNAPSHOT_NAME,
                "--description", package_marker,
            ], "package-ready snapshot creation failed", timeout=900)
            refreshed = subprocess.run(
                [vboxmanage, "showvminfo", expected_vm_uuid, "--machinereadable"],
                check=False, capture_output=True, text=True,
            )
            refreshed_info = parse_vm_info(refreshed.stdout)
            if refreshed.returncode != 0 or refreshed_info.get("CurrentSnapshotName") != PACKAGE_SNAPSHOT_NAME:
                raise RuntimeError("package-ready snapshot did not become current")
            package_snapshot_uuid = str(uuid.UUID(refreshed_info.get("CurrentSnapshotUUID", "")))
        run_checked(command, "vagrant package failed", environment, timeout=3600)
        if not arguments.output.is_file() or arguments.output.stat().st_size == 0:
            raise RuntimeError("Vagrant did not produce a nonempty box")
        arguments.output.chmod(0o600)
        if arguments.output.stat().st_mode & 0o777 != 0o600:
            raise RuntimeError("Vagrant box output mode is not 600")
        receipt = {
            "schema": "projectbuddy/ubuntu-vagrant-box@2",
            "source": {
                "vm": {"name": arguments.vm_name, "uuid": expected_vm_uuid},
                "activatedSnapshot": {"name": ACTIVATED_SNAPSHOT_NAME, "uuid": activated_snapshot_uuid},
                "snapshot": {"name": PACKAGE_SNAPSHOT_NAME, "uuid": package_snapshot_uuid},
            },
            "box": {
                "filename": arguments.output.name,
                "sha256": digest(arguments.output),
                "sizeBytes": arguments.output.stat().st_size,
            },
            "provenanceSha256": {name: digest(path) for name, path in files.items()},
            "supplyChain": {
                "ubuntuIsoSha256": manifest_data["os"]["iso"]["sha256"],
                "labviewRepository": manifest_data["labview"]["repository"],
                "labviewPackage": manifest_data["labview"]["package"],
                "labviewPackageVersion": manifest_data["labview"]["packageVersion"],
                "vipmVersion": manifest_data["vipm"]["version"],
                "vipmArtifactSha256": manifest_data["vipm"]["sha256"],
            },
            "runnerScan": runner_scan,
            "failures": [],
            "outcome": "PASS",
        }
        write_atomic(arguments.receipt_out, json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        run_checked([
            str(script_dir / "verify-vagrant-box-receipt.py"), str(arguments.receipt_out),
            "--box", str(arguments.output), "--manifest", str(manifest),
            "--base-receipt", str(files["baseReceiptSha256"]),
            "--public-receipt", str(files["publicProvisioningReceiptSha256"]),
            "--public-appliance-receipt", str(files["publicApplianceReceiptSha256"]),
            "--activation-receipt", str(files["activationReceiptSha256"]),
            "--package-report", str(arguments.package_report),
            "--package-artifact", str(arguments.package_artifact),
            "--embedded-vagrantfile", str(embedded_vagrantfile),
            "--vm-name", arguments.vm_name, "--vm-uuid", expected_vm_uuid,
            "--activated-snapshot-uuid", activated_snapshot_uuid,
            "--package-snapshot-uuid", package_snapshot_uuid,
            "--hash-out", str(receipt_hash_out),
        ], "Vagrant box receipt verification failed")
    except (KeyError, OSError, RuntimeError, ValueError) as error:
        arguments.output.unlink(missing_ok=True)
        arguments.receipt_out.unlink(missing_ok=True)
        receipt_hash_out.unlink(missing_ok=True)
        return fail(str(error), 1)

    print(f"Vagrant box packaging: PASS: {arguments.output}")
    print(f"Vagrant box packaging: PASS: {arguments.receipt_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())