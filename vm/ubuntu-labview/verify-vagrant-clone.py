#!/usr/bin/env python3
"""The LabVIEW appliance's clone check: the neutral clone verifier with ``clone-profile.json``.

The appliance's own checks stay here: the evidence directory must be on internal NVMe, the SSH host
key comes from the base receipt that the box receipt binds, and the box receipt is verified again
with ``verify-vagrant-box-receipt.py``. What the clone must show is the profile's data.
"""
import argparse
import pathlib
import re
import shutil
import sys
import uuid

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parents[1] / "scripts"))

from lane_box.common import LaneBoxError, sha256_file  # noqa: E402
from vagrant_clone import core, live  # noqa: E402
from vagrant_clone.profile import load_profile  # noqa: E402
from vagrant_clone.receipt import load_object, source_box  # noqa: E402

BOX_SCHEMA = "projectbuddy/ubuntu-vagrant-box@2"
UNSCANNED_BOX_SCHEMA = "projectbuddy/ubuntu-vagrant-box@1"
FINGERPRINT = re.compile(r"SHA256:[A-Za-z0-9+/]{43}")


def fail(message: str, code: int = 2) -> int:
    print(f"Vagrant clone verification: FAIL: {message}", file=sys.stderr)
    return code


def require_internal_nvme(path: pathlib.Path) -> None:
    code, output = live.run_command(["findmnt", "-n", "-o", "SOURCE", "-T", str(path)], timeout=60)
    devices = {line.strip() for line in output.splitlines() if line.strip().startswith("/dev/")}
    if code != 0 or len(devices) != 1:
        raise ValueError("cannot resolve clone evidence block storage")
    code, output = live.run_command(["lsblk", "-n", "-o", "TRAN", devices.pop()], timeout=60)
    if code != 0 or output.strip().lower() != "nvme":
        raise ValueError("clone evidence is not backed by internal NVMe")


def main() -> int:
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
    parser.add_argument("--profile", type=pathlib.Path, default=SCRIPT_DIR / "clone-profile.json")
    parser.add_argument("--vboxmanage")
    parser.add_argument("--run", action="store_true")
    arguments = parser.parse_args()

    try:
        profile = load_profile(arguments.profile)
        expected_vm_uuid = core.check_request(
            arguments.vm_name, arguments.vm_uuid, arguments.ssh_port,
            arguments.guest_password_file, arguments.evidence_dir,
        )
        require_internal_nvme(arguments.evidence_dir)
        core.check_machine_id(arguments.vagrant_machine_id_file, expected_vm_uuid)
        base_receipt = load_object(arguments.base_receipt, "base receipt")
        box_receipt = load_object(arguments.box_receipt, "box receipt")
        expected_fingerprint = base_receipt["sshHostKey"]["fingerprint"]
        if not isinstance(expected_fingerprint, str) or not FINGERPRINT.fullmatch(expected_fingerprint):
            raise ValueError("base receipt SSH fingerprint is invalid")
        if box_receipt.get("schema") == UNSCANNED_BOX_SCHEMA:
            raise ValueError("box was packaged without a runner scan (ubuntu-vagrant-box@1); repackage it")
        if box_receipt.get("schema") != BOX_SCHEMA or box_receipt.get("outcome") != "PASS":
            raise ValueError("box receipt is not a passing governed receipt")
        source_vm_uuid = str(uuid.UUID(box_receipt["source"]["vm"]["uuid"]))
        if expected_vm_uuid == source_vm_uuid:
            raise ValueError("clean clone UUID must differ from the source appliance UUID")
        if sha256_file(arguments.base_receipt) != box_receipt.get("provenanceSha256", {}).get("baseReceiptSha256"):
            raise ValueError("base receipt does not match box provenance")
        activated_snapshot_uuid = str(uuid.UUID(box_receipt["source"]["activatedSnapshot"]["uuid"]))
        package_snapshot_uuid = str(uuid.UUID(box_receipt["source"]["snapshot"]["uuid"]))
        box = source_box(arguments.box_receipt)
        live.check_outputs(profile, arguments.evidence_dir)
    except (AttributeError, KeyError, TypeError, OSError, ValueError, LaneBoxError) as error:
        return fail(str(error))

    vboxmanage = arguments.vboxmanage or shutil.which("VBoxManage")
    if not vboxmanage:
        return fail("VBoxManage is missing", 127)
    code, _ = live.run_command([
        str(SCRIPT_DIR / "verify-vagrant-box-receipt.py"), str(arguments.box_receipt),
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
    if code != 0:
        return fail("independent box receipt verification failed")
    clone = live.Clone(
        name=arguments.vm_name, uuid=expected_vm_uuid, ssh_port=arguments.ssh_port,
        fingerprint=expected_fingerprint, password_file=arguments.guest_password_file,
        evidence_dir=arguments.evidence_dir, vboxmanage=vboxmanage,
    )
    try:
        live.observe_instance(clone)
    except LaneBoxError as error:
        return fail(str(error))

    if not arguments.run:
        print("Vagrant clone verification: PLAN")
        print(f"clone_vm_uuid={expected_vm_uuid}")
        print(f"ssh_port={arguments.ssh_port}")
        print(f"source_box_sha256={box['boxSha256']}")
        print(f"clone_profile={profile.name} sha256={profile.sha256}")
        return 0
    try:
        receipt_path = live.run_clone_check(clone, profile, box, arguments.box_receipt)
    except (LaneBoxError, OSError) as error:
        return fail(str(error), 1)
    print(f"Vagrant clone verification: PASS: {receipt_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
