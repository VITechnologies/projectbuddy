#!/usr/bin/env python3
import argparse
import datetime
import hashlib
import json
import os
import pathlib
import re
import sys
import tarfile
import uuid
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))

from lane_box.readiness import SKIPPED_ROOTS  # noqa: E402

SCHEMA = "projectbuddy/ubuntu-vagrant-box@2"
UNSCANNED_SCHEMA = "projectbuddy/ubuntu-vagrant-box@1"
RUNNER_SCAN_FIELDS = {"bootId", "runnerUnits", "runnerConfigurations", "skippedRoots", "scannedAt"}
ACTIVATED_SNAPSHOT_NAME = "labview2026-q3-pro-activated-ready"
PACKAGE_SNAPSHOT_NAME = "labview2026-q3-pro-package-ready"
FORBIDDEN_FIELDS = {
    "activation_code", "credential", "email", "full_name", "fullname",
    "installation_id", "password", "private_key", "serial", "serial_number", "token",
}


def digest(path: pathlib.Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key.lower()
            yield from keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from keys(child)


def is_uuid(value) -> bool:
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def is_utc_time(value) -> bool:
    try:
        moment = datetime.datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return False
    return moment is not None and moment.utcoffset() == datetime.timedelta(0)


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
    parser = argparse.ArgumentParser()
    parser.add_argument("receipt", type=pathlib.Path)
    parser.add_argument("--box", type=pathlib.Path, required=True)
    parser.add_argument("--manifest", type=pathlib.Path, required=True)
    parser.add_argument("--base-receipt", type=pathlib.Path, required=True)
    parser.add_argument("--public-receipt", type=pathlib.Path, required=True)
    parser.add_argument("--public-appliance-receipt", type=pathlib.Path, required=True)
    parser.add_argument("--activation-receipt", type=pathlib.Path, required=True)
    parser.add_argument("--package-report", type=pathlib.Path, required=True)
    parser.add_argument("--package-artifact", type=pathlib.Path, required=True)
    parser.add_argument("--embedded-vagrantfile", type=pathlib.Path, required=True)
    parser.add_argument("--vm-name", required=True)
    parser.add_argument("--vm-uuid", required=True)
    parser.add_argument("--activated-snapshot-uuid", required=True)
    parser.add_argument("--package-snapshot-uuid", required=True)
    parser.add_argument("--hash-out", type=pathlib.Path)
    arguments = parser.parse_args()

    try:
        expected_vm_uuid = str(uuid.UUID(arguments.vm_uuid))
        activated_snapshot_uuid = str(uuid.UUID(arguments.activated_snapshot_uuid))
        package_snapshot_uuid = str(uuid.UUID(arguments.package_snapshot_uuid))
        if os.name != "nt":
            if arguments.receipt.stat().st_mode & 0o777 != 0o600:
                raise ValueError("receipt mode must be 600")
            if arguments.box.stat().st_mode & 0o777 != 0o600:
                raise ValueError("box mode must be 600")
        if arguments.hash_out is not None:
            if not arguments.hash_out.is_absolute() or arguments.hash_out.exists():
                raise ValueError("hash output must be an absent absolute path")
            if arguments.hash_out.parent != arguments.receipt.parent:
                raise ValueError("hash output must be beside the receipt")
        receipt = json.loads(arguments.receipt.read_text(encoding="utf-8"))
        manifest = json.loads(arguments.manifest.read_text(encoding="utf-8"))
        with tarfile.open(arguments.box, "r:*") as archive:
            members = archive.getmembers()
            wrappers = [candidate for candidate in members if candidate.name.lstrip("./") == "Vagrantfile"]
            policies = [candidate for candidate in members if candidate.name.lstrip("./") == "include/_Vagrantfile"]
            if len(wrappers) != 1 or not wrappers[0].isfile():
                raise ValueError("box must contain exactly one regular generated Vagrantfile")
            if len(policies) != 1 or not policies[0].isfile():
                raise ValueError("box must contain exactly one regular included Vagrantfile policy")
            wrapper_stream = archive.extractfile(wrappers[0])
            policy_stream = archive.extractfile(policies[0])
            if wrapper_stream is None or policy_stream is None:
                raise ValueError("box Vagrantfile content is unreadable")
            wrapper = wrapper_stream.read().decode("utf-8")
            embedded_hash = hashlib.sha256(policy_stream.read()).hexdigest()
            wrapper_pattern = re.compile(
                r'Vagrant::Config\.run do \|config\|\n'
                r'(?:  #.*\n){3}'
                r'  config\.vm\.base_mac = "[0-9A-F]{12}"\nend\n\n'
                r'# Load include vagrant file if it exists after the auto-generated\n'
                r'# so it can override any of the settings\n'
                r'include_vagrantfile = File\.expand_path\("\.\./include/_Vagrantfile", __FILE__\)\n'
                r'load include_vagrantfile if File\.exist\?\(include_vagrantfile\)\n?'
            )
            if not wrapper_pattern.fullmatch(wrapper):
                raise ValueError("generated Vagrantfile contains unexpected behavior")
        expected_provenance = {
            "activationReceiptSha256": digest(arguments.activation_receipt),
            "baseReceiptSha256": digest(arguments.base_receipt),
            "embeddedVagrantfileSha256": digest(arguments.embedded_vagrantfile),
            "manifestSha256": digest(arguments.manifest),
            "publicApplianceReceiptSha256": digest(arguments.public_appliance_receipt),
            "publicProvisioningReceiptSha256": digest(arguments.public_receipt),
            "packageBuildReportSha256": digest(arguments.package_report),
            "packageArtifactSha256": digest(arguments.package_artifact),
        }
    except (OSError, ValueError, json.JSONDecodeError, tarfile.TarError) as error:
        print(f"Vagrant box receipt: FAIL: {error}", file=sys.stderr)
        return 2

    errors = []

    def require(condition: bool, message: str) -> None:
        if not condition:
            errors.append(message)

    if receipt.get("schema") == UNSCANNED_SCHEMA:
        errors.append("box was packaged without a runner scan (ubuntu-vagrant-box@1); repackage it")
    else:
        require(receipt.get("schema") == SCHEMA, "unexpected schema")
    scan = receipt.get("runnerScan")
    require(isinstance(scan, dict) and set(scan) == RUNNER_SCAN_FIELDS, "runner scan is missing or malformed")
    if isinstance(scan, dict):
        require(scan.get("runnerUnits") == [] and scan.get("runnerConfigurations") == [],
                "runner scan found an Actions runner")
        require(scan.get("skippedRoots") == ["/" + name for name in SKIPPED_ROOTS], "runner scan skipped other roots")
        require(is_uuid(scan.get("bootId")), "runner scan boot ID is invalid")
        require(is_utc_time(scan.get("scannedAt")), "runner scan time is invalid")
    require(receipt.get("outcome") == "PASS", "outcome is not PASS")
    require(receipt.get("failures") == [], "receipt contains failures")
    require(receipt.get("source") == {
        "vm": {"name": arguments.vm_name, "uuid": expected_vm_uuid},
        "activatedSnapshot": {"name": ACTIVATED_SNAPSHOT_NAME, "uuid": activated_snapshot_uuid},
        "snapshot": {"name": PACKAGE_SNAPSHOT_NAME, "uuid": package_snapshot_uuid},
    }, "source VM or snapshot identity mismatch")
    require(receipt.get("box") == {
        "filename": arguments.box.name,
        "sha256": digest(arguments.box),
        "sizeBytes": arguments.box.stat().st_size,
    }, "box identity mismatch")
    require(receipt.get("provenanceSha256") == expected_provenance, "provenance hash mismatch")
    require(embedded_hash == expected_provenance["embeddedVagrantfileSha256"], "embedded Vagrantfile mismatch")
    require(receipt.get("supplyChain") == {
        "ubuntuIsoSha256": manifest["os"]["iso"]["sha256"],
        "labviewRepository": manifest["labview"]["repository"],
        "labviewPackage": manifest["labview"]["package"],
        "labviewPackageVersion": manifest["labview"]["packageVersion"],
        "vipmVersion": manifest["vipm"]["version"],
        "vipmArtifactSha256": manifest["vipm"]["sha256"],
    }, "supply-chain identity mismatch")
    forbidden = sorted(FORBIDDEN_FIELDS.intersection(keys(receipt)))
    require(not forbidden, f"forbidden fields present: {', '.join(forbidden)}")
    require(bool(re.fullmatch(r"[a-f0-9]{64}", receipt.get("box", {}).get("sha256", ""))), "box SHA-256 is invalid")

    if errors:
        for error in errors:
            print(f"Vagrant box receipt: FAIL: {error}", file=sys.stderr)
        return 2
    if arguments.hash_out is not None:
        try:
            write_atomic(arguments.hash_out, f"{digest(arguments.receipt)}  {arguments.receipt.name}\n")
        except OSError as error:
            print(f"Vagrant box receipt: FAIL: cannot write hash output: {error}", file=sys.stderr)
            return 2
    print(f"Vagrant box receipt: PASS: {arguments.box.name} ({package_snapshot_uuid})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())