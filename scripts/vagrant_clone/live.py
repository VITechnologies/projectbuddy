"""The clone check against a running clone, from host-key enrollment to the verified receipt.

Order: enroll the host key, list runner units (a ``refuse`` profile stops here), run the profile's
capture, check the evidence, remove the guest scratch directory, power off, then write and verify
the receipt. Whatever happens, the clone is left powered off when it can be, and the private
temporary directory is removed.
"""
from __future__ import annotations

import json
import os
import pathlib
import shlex
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Callable

from lane.common import write_new
from lane_box.common import LaneBoxError, hash_line

from . import guest
from .core import CloneError, check_instance, parse_vm_info
from .guest import ENROLL_SCRIPT, GUEST_HOST, GUEST_USER, QUERY_TIMEOUT, Run, run_command
from .profile import Profile, boot_id, failures, load_evidence
from .receipt import RECEIPT_FILENAME, build_receipt, verify_receipt

SCRATCH = f"/home/{GUEST_USER}/projectbuddy-clone-proof"
TEMPORARY_PREFIX = "projectbuddy-vagrant-clone-"
PREPARE_TIMEOUT, COPY_TIMEOUT = 90, 120


@dataclass(frozen=True)
class Clone:
    name: str
    uuid: str
    ssh_port: int
    fingerprint: str
    password_file: pathlib.Path
    evidence_dir: pathlib.Path
    vboxmanage: str

    @property
    def guest(self) -> guest.Guest:
        return guest.Guest(uuid=self.uuid, ssh_port=self.ssh_port, fingerprint=self.fingerprint,
                           password_file=self.password_file, vboxmanage=self.vboxmanage)


def receipt_paths(evidence_dir: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    return evidence_dir / RECEIPT_FILENAME, evidence_dir / f"{RECEIPT_FILENAME}.sha256"


def check_outputs(profile: Profile, evidence_dir: pathlib.Path) -> None:
    """The capture script must exist, and no receipt or evidence file may exist yet."""
    if not (profile.directory / profile.script).is_file():
        raise CloneError(f"clone capture script is missing: {profile.script}")
    if any(path.exists() for path in (*receipt_paths(evidence_dir), *(evidence_dir / name for name in profile.files))):
        raise CloneError("clone evidence output already exists")


def observe_instance(clone: Clone, run: Run = run_command) -> None:
    code, output = run([clone.vboxmanage, "showvminfo", clone.uuid, "--machinereadable"], timeout=QUERY_TIMEOUT)
    if code != 0:
        raise CloneError("cannot read clone VM identity")
    check_instance(parse_vm_info(output), clone.name, clone.uuid, clone.ssh_port)


def _capture(profile: Profile, clone: Clone, session: guest.Session) -> None:
    remote_script = f"{SCRATCH}/{profile.script}"
    if session.ssh(f"rm -rf {SCRATCH}; install -d -m 700 {SCRATCH}", PREPARE_TIMEOUT)[0] != 0:
        raise CloneError("cannot prepare clone proof directory")
    if session.scp(str(profile.directory / profile.script), f"{GUEST_USER}@{GUEST_HOST}:{remote_script}",
                   COPY_TIMEOUT) != 0:
        raise CloneError("cannot upload clone capture script")
    command = f"chmod 700 {remote_script} && {shlex.join([remote_script, '--output-dir', f'{SCRATCH}/evidence'])}"
    if session.ssh(command, profile.timeout)[0] != 0:
        raise CloneError("clone runtime capture failed")
    for name in profile.files:
        target = clone.evidence_dir / name
        if session.scp(f"{GUEST_USER}@{GUEST_HOST}:{SCRATCH}/evidence/{name}", str(target), COPY_TIMEOUT) != 0:
            raise CloneError(f"cannot retrieve clone evidence: {name}")
        target.chmod(0o600)


def run_clone_check(clone: Clone, profile: Profile, box: dict, box_receipt: pathlib.Path, *,
                    run: Run = run_command, sleep: Callable[[float], None] = time.sleep,
                    enroll_script: pathlib.Path = ENROLL_SCRIPT,
                    verify: Callable[..., dict] = verify_receipt, **verify_options) -> pathlib.Path:
    """Check a running clone and return its verified receipt's path; raise ``CloneError`` on any failure.

    ``box`` is the receipt's ``sourceBox`` block for ``box_receipt``. ``verify_options`` go to ``verify``.
    """
    receipt_path, hash_path = receipt_paths(clone.evidence_dir)
    root = pathlib.Path(tempfile.mkdtemp(prefix=TEMPORARY_PREFIX, dir=os.environ.get("XDG_RUNTIME_DIR")))
    written: list[pathlib.Path] = []
    powered_off = False
    try:
        session = guest.Session(clone.guest, root, run)
        guest.enroll(clone.guest, session, run, sleep, enroll_script)
        observed = guest.list_runner_units(session)
        if observed and profile.runner_units == "refuse":
            raise CloneError("REQ-103: the clone has an Actions runner unit: " + ", ".join(observed))
        _capture(profile, clone, session)
        evidence = load_evidence(profile, clone.evidence_dir)
        failed = failures(profile, evidence)
        if failed:
            raise CloneError("; ".join(failed))
        receipt = build_receipt(profile, box=box, name=clone.name, uuid=clone.uuid,
                                boot=boot_id(profile, evidence), evidence_dir=clone.evidence_dir, observed=observed)
        if session.ssh(f"rm -rf {SCRATCH}", PREPARE_TIMEOUT)[0] != 0:
            raise CloneError("clone guest evidence cleanup failed")
        powered_off = guest.power_off(clone.guest, session, run, sleep)
        if not powered_off:
            raise CloneError("clone did not power off cleanly")
        write_new(receipt_path, json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        written.append(receipt_path)
        try:
            verify(receipt_path, profile=profile, box_receipt=box_receipt, evidence_dir=clone.evidence_dir,
                   name=clone.name, uuid=clone.uuid, **verify_options)
        except LaneBoxError as error:
            raise CloneError(f"independent clone receipt verification failed: {error}") from error
        write_new(hash_path, hash_line(receipt_path))
        written.append(hash_path)
    except (LaneBoxError, OSError) as error:
        for path in written:
            path.unlink(missing_ok=True)
        if isinstance(error, CloneError):
            raise
        raise CloneError(str(error)) from error
    finally:
        if not powered_off and not guest.press_power_button(clone.guest, run, sleep):
            print("Vagrant clone verification: WARN: clone did not power off during cleanup", file=sys.stderr)
        shutil.rmtree(root, ignore_errors=True)
    return receipt_path
