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
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Callable

from lane.common import write_new
from lane_box.common import LaneBoxError, hash_line

from .core import RUNNER_UNIT_QUERIES, CloneError, check_instance, parse_vm_info, runner_units
from .profile import Profile, boot_id, failures, load_evidence
from .receipt import RECEIPT_FILENAME, build_receipt, verify_receipt

ENROLL_SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "vm" / "ubuntu-labview" / "enroll-ssh-host-key.py"
GUEST_USER = "actor"
GUEST_HOST = "127.0.0.1"
SCRATCH = f"/home/{GUEST_USER}/projectbuddy-clone-proof"
TEMPORARY_PREFIX = "projectbuddy-vagrant-clone-"
ENROLL_ATTEMPTS, ENROLL_INTERVAL, ENROLL_TIMEOUT = 30, 5, 20
POWEROFF_POLLS, POWEROFF_INTERVAL, POWEROFF_TIMEOUT = 60, 2, 30
QUERY_TIMEOUT, PREPARE_TIMEOUT, COPY_TIMEOUT = 60, 90, 120

Run = Callable[..., "tuple[int, str]"]


def run_command(argv: list[str], *, timeout: int, environment: dict[str, str] | None = None,
                stdin=subprocess.DEVNULL) -> tuple[int, str]:
    """Run ``argv`` and return its exit code and standard output; 124 is a timeout, 127 a missing program."""
    try:
        completed = subprocess.run(argv, check=False, capture_output=True, text=True, env=environment, stdin=stdin,
                                   start_new_session=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, ""
    except OSError:
        return 127, ""
    return completed.returncode, completed.stdout


@dataclass(frozen=True)
class Clone:
    name: str
    uuid: str
    ssh_port: int
    fingerprint: str
    password_file: pathlib.Path
    evidence_dir: pathlib.Path
    vboxmanage: str


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


class _Session:
    """ssh and scp to the clone with its enrolled host key and the guest password through askpass."""

    def __init__(self, clone: Clone, root: pathlib.Path, run: Run) -> None:
        self.run = run
        self.known_hosts = root / "known-hosts"
        askpass = root / "askpass.sh"
        askpass.write_text(f"#!/bin/sh\ncat {shlex.quote(str(clone.password_file))}\n", encoding="utf-8")
        askpass.chmod(0o700)
        self.environment = dict(os.environ, DISPLAY=":0", SSH_ASKPASS=str(askpass), SSH_ASKPASS_REQUIRE="force")
        options = ["-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={self.known_hosts}",
                   "-o", "ConnectTimeout=10"]
        self.ssh_argv = ["ssh", "-p", str(clone.ssh_port), *options, f"{GUEST_USER}@{GUEST_HOST}"]
        self.scp_argv = ["scp", "-P", str(clone.ssh_port), *options]

    def ssh(self, command: list[str] | str, timeout: int, stdin=subprocess.DEVNULL) -> tuple[int, str]:
        text = command if isinstance(command, str) else shlex.join(command)
        return self.run([*self.ssh_argv, text], timeout=timeout, environment=self.environment, stdin=stdin)

    def scp(self, source: str, target: str, timeout: int) -> int:
        return self.run([*self.scp_argv, source, target], timeout=timeout, environment=self.environment)[0]


def _enroll(clone: Clone, session: _Session, run: Run, sleep: Callable[[float], None],
            enroll_script: pathlib.Path) -> None:
    for attempt in range(ENROLL_ATTEMPTS):
        session.known_hosts.unlink(missing_ok=True)
        code, _ = run([str(enroll_script), "--host", GUEST_HOST, "--port", str(clone.ssh_port),
                       "--expected-fingerprint", clone.fingerprint, "--out", str(session.known_hosts)],
                      timeout=ENROLL_TIMEOUT)
        if code == 0:
            return
        sleep(ENROLL_INTERVAL)
    raise CloneError("clone SSH identity did not become ready")


def _capture(profile: Profile, clone: Clone, session: _Session) -> None:
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


def _await_poweroff(clone: Clone, run: Run, sleep: Callable[[float], None]) -> bool:
    for _ in range(POWEROFF_POLLS):
        _, output = run([clone.vboxmanage, "showvminfo", clone.uuid, "--machinereadable"], timeout=QUERY_TIMEOUT)
        if parse_vm_info(output).get("VMState") == "poweroff":
            return True
        sleep(POWEROFF_INTERVAL)
    return False


def _press_power_button(clone: Clone, run: Run, sleep: Callable[[float], None]) -> bool:
    run([clone.vboxmanage, "controlvm", clone.uuid, "acpipowerbutton"], timeout=QUERY_TIMEOUT)
    return _await_poweroff(clone, run, sleep)


def _power_off(clone: Clone, session: _Session, run: Run, sleep: Callable[[float], None]) -> bool:
    with clone.password_file.open("r", encoding="utf-8") as password:
        session.ssh('sudo -S -p "" systemctl poweroff', POWEROFF_TIMEOUT, stdin=password)
    return _await_poweroff(clone, run, sleep) or _press_power_button(clone, run, sleep)


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
        session = _Session(clone, root, run)
        _enroll(clone, session, run, sleep, enroll_script)
        observed = runner_units([session.ssh(list(query), QUERY_TIMEOUT) for query in RUNNER_UNIT_QUERIES])
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
        powered_off = _power_off(clone, session, run, sleep)
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
        if not powered_off and not _press_power_button(clone, run, sleep):
            print("Vagrant clone verification: WARN: clone did not power off during cleanup", file=sys.stderr)
        shutil.rmtree(root, ignore_errors=True)
    return receipt_path
