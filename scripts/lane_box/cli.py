"""Command line for the lane box: guest-side install and readiness, host-side package and verify."""
from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import sys
import tempfile

from .access import configure_access
from .common import ChangedError, LaneBoxError, canonical_uuid
from .packager import Hypervisor, Request, package
from .profile import load_profile
from .readiness import build_readiness, write_readiness
from .receipt import verify_box_receipt
from .toolchain import Fetcher, Runner, https_fetch, install_toolchain, run_command

REPOSITORY = pathlib.Path(__file__).resolve().parents[2]
EMBEDDED_VAGRANTFILE = REPOSITORY / "vm" / "ubuntu-lane" / "Vagrantfile.box"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="Invoke-LaneBox.py",
        description="Build and verify the LabVIEW-free Ubuntu lane box (docs/vm-provisioning/ubuntu-lane-box.md).",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate-profile", help="check a lane profile")
    validate.add_argument("profile", type=pathlib.Path)

    install = commands.add_parser("install-toolchain", help="guest, as root: verify every digest, then install")
    install.add_argument("--profile", type=pathlib.Path, required=True)
    install.add_argument("--workdir", type=pathlib.Path, help="download directory (default: a new private temporary directory)")
    install.add_argument("--prefix", type=pathlib.Path, default=pathlib.Path("/opt/projectbuddy-lane"))
    install.add_argument("--bindir", type=pathlib.Path, default=pathlib.Path("/usr/local/bin"))

    access = commands.add_parser("configure-access",
                                 help="guest, as root: install key-only root SSH access for the next boot")
    access.add_argument("--root", type=pathlib.Path, default=pathlib.Path("/"))

    readiness = commands.add_parser("readiness", help="guest, as root: scan and write the readiness receipt")
    readiness.add_argument("--profile", type=pathlib.Path, required=True)
    readiness.add_argument("--root", type=pathlib.Path, default=pathlib.Path("/"))
    readiness.add_argument("--output-dir", type=pathlib.Path, default=pathlib.Path("/var/lib/projectbuddy"))

    pack = commands.add_parser("package", help="host: package a temporary clone of the VM's current snapshot")
    pack.add_argument("--profile", type=pathlib.Path, required=True)
    pack.add_argument("--vm-name", required=True)
    pack.add_argument("--vm-uuid", required=True)
    pack.add_argument("--snapshot-name", required=True)
    pack.add_argument("--snapshot-uuid", required=True)
    pack.add_argument("--evidence-dir", type=pathlib.Path, required=True)
    pack.add_argument("--output", type=pathlib.Path, required=True)
    pack.add_argument("--receipt-out", type=pathlib.Path, required=True)
    pack.add_argument("--vagrant-home", type=pathlib.Path, required=True)
    pack.add_argument("--temp-dir", type=pathlib.Path, required=True)
    pack.add_argument("--embedded-vagrantfile", type=pathlib.Path, default=EMBEDDED_VAGRANTFILE)
    pack.add_argument("--vboxmanage")
    pack.add_argument("--vagrant")
    pack.add_argument("--run", action="store_true", help="package for real (default: print the plan)")

    verify = commands.add_parser("verify-receipt", help="host: recompute and check a lane box receipt")
    verify.add_argument("receipt", type=pathlib.Path)
    verify.add_argument("--box", type=pathlib.Path, required=True)
    verify.add_argument("--profile", type=pathlib.Path, required=True)
    verify.add_argument("--base-receipt", type=pathlib.Path, required=True)
    verify.add_argument("--readiness-receipt", type=pathlib.Path, required=True)
    verify.add_argument("--embedded-vagrantfile", type=pathlib.Path, default=EMBEDDED_VAGRANTFILE)
    verify.add_argument("--vm-name")
    verify.add_argument("--vm-uuid")
    verify.add_argument("--snapshot-name")
    verify.add_argument("--snapshot-uuid")
    return parser


def _identity(name: str | None, value: str | None, what: str) -> dict | None:
    if name is None and value is None:
        return None
    if name is None or value is None:
        raise LaneBoxError(f"--{what}-name and --{what}-uuid go together")
    return {"name": name, "uuid": canonical_uuid(value, f"{what} UUID")}


def _install(arguments: argparse.Namespace, fetch: Fetcher, run: Runner) -> int:
    profile = load_profile(arguments.profile)
    workdir = arguments.workdir or pathlib.Path(tempfile.mkdtemp(prefix="lane-toolchain-"))
    try:
        results = install_toolchain(profile, workdir=workdir, prefix=arguments.prefix, bindir=arguments.bindir,
                                    fetch=fetch, run=run)
    finally:
        if arguments.workdir is None:
            shutil.rmtree(workdir, ignore_errors=True)
    for result in results:
        print(f"{result['name']}={result['observed']}")
    print(f"Lane toolchain: PASS: {profile.name}")
    return 0


def _configure_access(arguments: argparse.Namespace) -> int:
    if not arguments.root.is_absolute():
        raise LaneBoxError("--root must be an absolute path")
    for path in configure_access(arguments.root):
        print(f"installed {path}")
    print("Lane access: PASS: key-only root SSH takes effect at the next boot")
    return 0


def _readiness(arguments: argparse.Namespace, run: Runner) -> int:
    if not arguments.root.is_absolute():
        raise LaneBoxError("--root must be an absolute path")
    receipt = build_readiness(arguments.root, load_profile(arguments.profile), run)
    path = write_readiness(receipt, arguments.output_dir)
    for failure in receipt["failures"]:
        print(f"Lane readiness: FAIL: {failure}", file=sys.stderr)
    print(f"Lane readiness: {receipt['outcome']}: {path}")
    return 0 if receipt["outcome"] == "PASS" else 1


def _package(arguments: argparse.Namespace) -> int:
    vboxmanage = shutil.which(arguments.vboxmanage or "VBoxManage")
    vagrant = shutil.which(arguments.vagrant or "vagrant")
    if not vboxmanage or not vagrant:
        print("Lane box packaging: FAIL: VBoxManage or Vagrant is missing", file=sys.stderr)
        return 127
    request = Request(
        profile=arguments.profile, vm_name=arguments.vm_name, vm_uuid=arguments.vm_uuid,
        snapshot_name=arguments.snapshot_name, snapshot_uuid=arguments.snapshot_uuid,
        evidence_dir=arguments.evidence_dir, output=arguments.output, receipt_out=arguments.receipt_out,
        vagrant_home=arguments.vagrant_home, temp_dir=arguments.temp_dir,
        embedded_vagrantfile=arguments.embedded_vagrantfile, run=arguments.run,
    )
    previous = os.umask(0o077)
    try:
        lines = package(request, Hypervisor(vboxmanage, vagrant))
    finally:
        os.umask(previous)
    print("\n".join(lines))
    return 0


def _verify(arguments: argparse.Namespace) -> int:
    receipt = verify_box_receipt(
        arguments.receipt, box=arguments.box, profile_path=arguments.profile,
        base_receipt=arguments.base_receipt, readiness_receipt=arguments.readiness_receipt,
        embedded_vagrantfile=arguments.embedded_vagrantfile,
        expected_vm=_identity(arguments.vm_name, arguments.vm_uuid, "vm"),
        expected_snapshot=_identity(arguments.snapshot_name, arguments.snapshot_uuid, "snapshot"),
    )
    print(f"Lane box receipt: PASS: {receipt['box']['filename']} ({receipt['source']['snapshot']['uuid']})")
    return 0


def main(argv: list[str] | None = None, *, fetch: Fetcher = https_fetch, run: Runner = run_command) -> int:
    arguments = build_parser().parse_args(argv)
    label = {
        "validate-profile": "Lane profile", "install-toolchain": "Lane toolchain", "configure-access": "Lane access",
        "readiness": "Lane readiness", "package": "Lane box packaging", "verify-receipt": "Lane box receipt",
    }[arguments.command]
    try:
        if arguments.command == "validate-profile":
            profile = load_profile(arguments.profile)
            print(f"Lane profile: PASS: {profile.name} ({profile.sha256})")
            return 0
        if arguments.command == "install-toolchain":
            return _install(arguments, fetch, run)
        if arguments.command == "configure-access":
            return _configure_access(arguments)
        if arguments.command == "readiness":
            return _readiness(arguments, run)
        if arguments.command == "package":
            return _package(arguments)
        return _verify(arguments)
    except LaneBoxError as error:
        print(f"{label}: FAIL: {error}", file=sys.stderr)
        return 1 if isinstance(error, ChangedError) else 2
