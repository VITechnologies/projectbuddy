"""Host-side lane box packager. Dry run by default; ``run=True`` packages a temporary clone.

The operator takes the named snapshot after the readiness scan and powers the VM off;
the packager refuses unless that snapshot is current. With ``run=True`` it makes a full
clone of that snapshot under the temporary directory, runs ``vagrant package`` on the
clone, and deletes the clone. The source VM is only read: ``vagrant package`` deletes
the port forwards and shared folders of the VM it packages, and the clone holds the
snapshot even if the source VM was booted or changed after the snapshot was taken.
"""
from __future__ import annotations

import contextlib
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import uuid
from dataclasses import dataclass
from typing import Iterator

from .common import (
    VM_NAME_PATTERN, ChangedError, LaneBoxError, canonical_uuid, hash_line, require_private_directory,
    verify_hash_file, write_private,
)
from .profile import Profile, load_profile
from .readiness import READINESS_FILENAME
from .receipt import (
    build_box_receipt, check_base_receipt, check_readiness_receipt, load_json, provenance_inputs,
    verify_box_receipt,
)

BASE_RECEIPT_FILENAME = "base-bootstrap-receipt.json"
CLONE_TIMEOUT = 3600
PACKAGE_TIMEOUT = 3600
DELETE_TIMEOUT = 600
QUERY_TIMEOUT = 120
STOP_GRACE = 30
CLONE_PREFIX = "lane-pack-"


@dataclass(frozen=True)
class Request:
    profile: pathlib.Path
    vm_name: str
    vm_uuid: str
    snapshot_name: str
    snapshot_uuid: str
    evidence_dir: pathlib.Path
    output: pathlib.Path
    receipt_out: pathlib.Path
    vagrant_home: pathlib.Path
    temp_dir: pathlib.Path
    embedded_vagrantfile: pathlib.Path
    run: bool = False

    @property
    def receipt_hash(self) -> pathlib.Path:
        return pathlib.Path(f"{self.receipt_out}.sha256")


@dataclass(frozen=True)
class Checked:
    profile: Profile
    vm: dict
    snapshot: dict
    base_receipt: pathlib.Path
    readiness_receipt: pathlib.Path
    base: dict
    readiness: dict


def parse_vm_info(output: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in output.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip().strip('"')] = value.strip().strip('"')
    return values


class Hypervisor:
    """VBoxManage and Vagrant, called without a shell."""

    def __init__(self, vboxmanage: str, vagrant: str) -> None:
        self.vboxmanage = vboxmanage
        self.vagrant = vagrant
        self.stop_unconfirmed = False

    def _query(self, argv: list[str], message: str) -> str:
        try:
            completed = subprocess.run(
                argv, capture_output=True, text=True, timeout=QUERY_TIMEOUT, stdin=subprocess.DEVNULL, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise LaneBoxError(f"{message}: {error}") from error
        if completed.returncode != 0:
            raise LaneBoxError(message)
        return completed.stdout

    def vm_info(self, vm: str) -> dict[str, str]:
        return parse_vm_info(self._query([self.vboxmanage, "showvminfo", vm, "--machinereadable"],
                                         "cannot read VM identity"))

    def disk_capacity_mb(self, image_uuid: str) -> int:
        output = self._query([self.vboxmanage, "showmediuminfo", "disk", image_uuid], "cannot read the VM disk")
        match = re.search(r"^Capacity:\s+(\d+) MBytes$", output, re.MULTILINE)
        if not match:
            raise LaneBoxError("cannot read the VM disk capacity")
        return int(match.group(1))

    def registered_uuids(self) -> set[str]:
        output = self._query([self.vboxmanage, "list", "vms"], "cannot list the registered VMs")
        return set(re.findall(r"\{([0-9a-fA-F-]{36})\}\s*$", output, re.MULTILINE))

    def clone_command(self, checked: Checked, clone: dict, base_folder: pathlib.Path) -> list[str]:
        # A full clone of the snapshot alone; the MAC addresses stay so the box's base_mac matches the source.
        return [
            self.vboxmanage, "clonevm", checked.vm["uuid"], f"--snapshot={checked.snapshot['uuid']}",
            "--mode=machine", "--options=KeepAllMACs,KeepHwUUIDs", f"--name={clone['name']}",
            f"--uuid={clone['uuid']}", f"--basefolder={base_folder}", "--register",
        ]

    def package_command(self, request: Request, vm_uuid: str) -> list[str]:
        return [
            self.vagrant, "package", "--base", vm_uuid,
            "--output", str(request.output), "--vagrantfile", str(request.embedded_vagrantfile),
        ]

    def delete_command(self, vm_uuid: str) -> list[str]:
        return [self.vboxmanage, "unregistervm", vm_uuid, "--delete"]

    def run_checked(self, command: list[str], environment: dict[str, str], what: str, timeout: int) -> None:
        """Run ``command`` in its own process group; stop the whole group on a timeout or an interrupt.

        Afterwards ``stop_unconfirmed`` is true when the command may still have work running in
        VirtualBox: it was stopped and did not exit by itself, or the call ended without a result.
        """
        self.stop_unconfirmed = True
        process = subprocess.Popen(command, env=environment, start_new_session=True, stdin=subprocess.DEVNULL)
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.stop_unconfirmed = _stop(process)
            raise LaneBoxError(f"{what} timed out") from None
        except BaseException:
            self.stop_unconfirmed = _stop(process)
            raise
        self.stop_unconfirmed = False
        if returncode != 0:
            raise LaneBoxError(f"{what} failed with exit code {returncode}")


def _stop(process: subprocess.Popen) -> bool:
    """Send the group SIGTERM, then SIGKILL after a grace period; return True unless the child exited by itself.

    On SIGTERM, VBoxManage cancels its VirtualBox job, waits until VirtualBox has removed what the job
    created, and exits with a failure code. A child that dies from a signal may leave that job running.
    """
    # The group can already be gone when an interrupt arrives just as the child exits.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        returncode = process.wait(timeout=STOP_GRACE)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        # A second interrupt during the grace period kills the group at once.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        return True
    return returncode < 0


@contextlib.contextmanager
def _sigterm_interrupts() -> Iterator[None]:
    """Turn SIGTERM into ``KeyboardInterrupt`` so the cleanup below also runs when the packager is terminated."""
    def interrupt(signum: int, frame: object) -> None:
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, interrupt)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def _require_private_file(path: pathlib.Path, what: str) -> None:
    if not path.is_file():
        raise LaneBoxError(f"{what} is missing: {path}")
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise LaneBoxError(f"{what} must have mode 600 or stricter: {path}")


def check_vm(info: dict[str, str], vm: dict, snapshot: dict) -> None:
    if info.get("UUID") != vm["uuid"] or info.get("name") != vm["name"]:
        raise LaneBoxError("registered VM name or UUID does not match the expected identity")
    if info.get("VMState") != "poweroff":
        raise LaneBoxError("source VM must be powered off")
    if (info.get("CurrentSnapshotName"), info.get("CurrentSnapshotUUID")) != (snapshot["name"], snapshot["uuid"]):
        raise LaneBoxError(f"snapshot {snapshot['name']} ({snapshot['uuid']}) is not the current snapshot")


def check_hardware(info: dict[str, str], profile: Profile, hypervisor: Hypervisor) -> None:
    """The receipt records the profile's hardware, so the VM and its clone must have exactly that hardware."""
    declared = profile.virtual_box
    observed = {"memoryMb": info.get("memory"), "cpus": info.get("cpus"), "vramMb": info.get("vram")}
    mismatches = [f"{field} is {value}, the profile declares {declared[field]}"
                  for field, value in observed.items() if value != str(declared[field])]
    # build-virtualbox.sh attaches the system disk at SATA port 0, device 0.
    image = info.get("SATA-ImageUUID-0-0")
    if not image:
        mismatches.append("no disk is attached at SATA port 0")
    else:
        capacity = hypervisor.disk_capacity_mb(canonical_uuid(image, "disk UUID"))
        if capacity != declared["diskGb"] * 1024:
            mismatches.append(f"disk is {capacity} MB, the profile declares {declared['diskGb'] * 1024} MB")
    if mismatches:
        raise LaneBoxError("VM hardware does not match the lane profile: " + "; ".join(mismatches))


def preflight(request: Request, hypervisor: Hypervisor) -> Checked:
    """Run every refusal; nothing has been changed when this raises."""
    for name, what in ((request.vm_name, "VM name"), (request.snapshot_name, "snapshot name")):
        if not VM_NAME_PATTERN.fullmatch(name):
            raise LaneBoxError(f"{what} is invalid")
    vm = {"name": request.vm_name, "uuid": canonical_uuid(request.vm_uuid, "VM UUID")}
    snapshot = {"name": request.snapshot_name, "uuid": canonical_uuid(request.snapshot_uuid, "snapshot UUID")}
    for path in (request.output, request.receipt_out):
        if not path.is_absolute():
            raise LaneBoxError(f"output paths must be absolute: {path}")
    for directory in (request.evidence_dir, request.vagrant_home, request.temp_dir,
                      request.output.parent, request.receipt_out.parent):
        require_private_directory(directory)
    for path in (request.output, request.receipt_out, request.receipt_hash):
        if path.exists() or path.is_symlink():
            raise LaneBoxError(f"output already exists: {path}")
    if not request.embedded_vagrantfile.is_file():
        raise LaneBoxError(f"embedded Vagrantfile is missing: {request.embedded_vagrantfile}")
    profile = load_profile(request.profile)
    base_receipt = request.evidence_dir / BASE_RECEIPT_FILENAME
    readiness_receipt = request.evidence_dir / READINESS_FILENAME
    _require_private_file(base_receipt, "base receipt")
    _require_private_file(readiness_receipt, "readiness receipt")
    verify_hash_file(request.evidence_dir / f"{READINESS_FILENAME}.sha256", readiness_receipt)
    base = load_json(base_receipt, "base receipt")
    readiness = load_json(readiness_receipt, "readiness receipt")
    errors = check_base_receipt(base, vm["name"], vm["uuid"])
    errors += check_readiness_receipt(readiness, profile, vm["name"], vm["uuid"])
    if errors:
        raise LaneBoxError("; ".join(errors))
    info = hypervisor.vm_info(vm["name"])
    check_vm(info, vm, snapshot)
    check_hardware(info, profile, hypervisor)
    return Checked(profile, vm, snapshot, base_receipt, readiness_receipt, base, readiness)


def _clone_warning(clone: dict, clone_dir: pathlib.Path) -> str:
    return (f"the temporary clone {clone['name']} ({clone['uuid']}) may remain: if VBoxManage list vms shows it, "
            f"run VBoxManage unregistervm {clone['uuid']} --delete, then remove {clone_dir}")


def _remove_clone(hypervisor: Hypervisor, clone: dict, clone_dir: pathlib.Path,
                  environment: dict[str, str]) -> str | None:
    """Delete the clone and its directory; return ``None`` when both are gone, otherwise a warning."""
    try:
        if clone["uuid"] in hypervisor.registered_uuids():
            hypervisor.run_checked(hypervisor.delete_command(clone["uuid"]), environment,
                                   "VBoxManage unregistervm", DELETE_TIMEOUT)
        if clone_dir.exists():
            shutil.rmtree(clone_dir)
    except (LaneBoxError, OSError) as error:
        return f"{_clone_warning(clone, clone_dir)}: {error}"
    return None


def package(request: Request, hypervisor: Hypervisor) -> list[str]:
    """Return the plan (dry run) or the produced paths (run).

    Raises ``LaneBoxError`` on a refusal, before anything changed, and ``ChangedError`` on a later failure
    or interrupt, after the outputs and the temporary clone were removed.
    """
    checked = preflight(request, hypervisor)
    if not request.run:
        placeholder = {"name": f"{CLONE_PREFIX}<new-uuid>", "uuid": "<new-uuid>"}
        return [
            "Lane box packaging: PLAN",
            "clone=" + " ".join(hypervisor.clone_command(checked, placeholder, request.temp_dir)),
            "package=" + " ".join(hypervisor.package_command(request, placeholder["uuid"])),
            "delete=" + " ".join(hypervisor.delete_command(placeholder["uuid"])),
            f"profile={checked.profile.name} ({checked.profile.sha256})",
            f"source_vm={checked.vm['name']} ({checked.vm['uuid']})",
            f"snapshot={checked.snapshot['name']} ({checked.snapshot['uuid']})",
            f"checked_boot_id={checked.readiness['bootId']}",
        ]
    info = hypervisor.vm_info(checked.vm["uuid"])
    check_vm(info, checked.vm, checked.snapshot)
    check_hardware(info, checked.profile, hypervisor)
    clone_uuid = str(uuid.uuid4())
    clone = {"name": f"{CLONE_PREFIX}{clone_uuid}", "uuid": clone_uuid}
    clone_dir = request.temp_dir / clone["name"]
    if clone_dir.exists() or clone_dir.is_symlink():
        raise LaneBoxError(f"temporary clone directory already exists: {clone_dir}")
    environment = dict(os.environ, VAGRANT_HOME=str(request.vagrant_home), TMPDIR=str(request.temp_dir))
    leftover: str | None = _clone_warning(clone, clone_dir)
    cloning = True
    with _sigterm_interrupts():
        try:
            try:
                hypervisor.run_checked(hypervisor.clone_command(checked, clone, request.temp_dir), environment,
                                       "VBoxManage clonevm", CLONE_TIMEOUT)
                cloning = False
                check_hardware(hypervisor.vm_info(clone["uuid"]), checked.profile, hypervisor)
                hypervisor.run_checked(hypervisor.package_command(request, clone["uuid"]), environment,
                                       "vagrant package", PACKAGE_TIMEOUT)
            finally:
                # A clone job that VBoxManage did not see cancelled can still write and register the clone later.
                unconfirmed = cloning and hypervisor.stop_unconfirmed
                leftover = _remove_clone(hypervisor, clone, clone_dir, environment)
                if unconfirmed and not leftover:
                    leftover = (f"{_clone_warning(clone, clone_dir)}: VBoxManage clonevm stopped before "
                                "VirtualBox confirmed the cancel")
            if leftover:
                raise LaneBoxError(leftover)
            if not request.output.is_file() or request.output.stat().st_size == 0:
                raise LaneBoxError("Vagrant did not produce a nonempty box")
            request.output.chmod(0o600)
            inputs = provenance_inputs(request.profile, checked.base_receipt, checked.readiness_receipt,
                                       request.embedded_vagrantfile)
            receipt = build_box_receipt(
                profile=checked.profile, vm=checked.vm, snapshot=checked.snapshot, box=request.output,
                inputs=inputs, base=checked.base, readiness=checked.readiness,
            )
            write_private(request.receipt_out, json_text(receipt))
            verify_box_receipt(
                request.receipt_out, box=request.output, profile_path=request.profile,
                base_receipt=checked.base_receipt, readiness_receipt=checked.readiness_receipt,
                embedded_vagrantfile=request.embedded_vagrantfile,
                expected_vm=checked.vm, expected_snapshot=checked.snapshot,
            )
            write_private(request.receipt_hash, hash_line(request.receipt_out))
        except BaseException as error:
            for path in (request.output, request.receipt_out, request.receipt_hash):
                path.unlink(missing_ok=True)
            if not isinstance(error, (LaneBoxError, OSError, KeyboardInterrupt)):
                raise
            detail = "interrupted" if isinstance(error, KeyboardInterrupt) else str(error)
            if leftover and leftover not in detail:
                detail = f"{detail}; {leftover}"
            raise ChangedError(f"packaging failed; outputs were removed: {detail}") from error
    return [f"Lane box packaging: PASS: {request.output}", f"Lane box packaging: PASS: {request.receipt_out}"]


def json_text(value: dict) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"
