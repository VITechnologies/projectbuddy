"""A running VirtualBox guest reached over its loopback SSH forward.

Host-key enrollment, ssh and scp with the guest password through askpass, the Actions runner scan, and power-off.
The clone check (``live``) and the appliance packager (``vm/ubuntu-labview/package-vagrant-box.py``) both use it, so
they reach a guest, decide what a runner is, and power the guest off the same way.
"""
from __future__ import annotations

import os
import pathlib
import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable

from lane_box.common import LaneBoxError, canonical_uuid
from lane_box.readiness import BOOT_ID_PATH, RUNNER_FILES, SKIPPED_ROOTS

from .core import RUNNER_UNIT_QUERIES, parse_vm_info, runner_units

ENROLL_SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "vm" / "ubuntu-labview" / "enroll-ssh-host-key.py"
GUEST_USER = "actor"
GUEST_HOST = "127.0.0.1"
ENROLL_ATTEMPTS, ENROLL_INTERVAL, ENROLL_TIMEOUT = 30, 5, 20
POWEROFF_POLLS, POWEROFF_INTERVAL, POWEROFF_TIMEOUT = 60, 2, 30
QUERY_TIMEOUT, SCAN_TIMEOUT = 60, 900

Run = Callable[..., "tuple[int, str]"]


def _either(test: str, values: Iterable[str]) -> list[str]:
    words: list[str] = []
    for value in sorted(values):
        words += [*(["-o"] if words else []), test, value]
    return words


# The readiness scan's rule (``lane_box.readiness``): an entry named .credentials or .runner anywhere except the
# pseudo-filesystems. find does not follow symbolic links by default, and exits nonzero if any part is unreadable.
RUNNER_CONFIGURATION_QUERY = (
    "find", "/", "(", *_either("-path", ("/" + name for name in SKIPPED_ROOTS)), ")", "-prune",
    "-o", "(", *_either("-name", RUNNER_FILES), ")", "-print",
)


class GuestError(LaneBoxError):
    pass


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
class Guest:
    """A running VM: its UUID, its loopback SSH port, its expected host key, its guest password file, VBoxManage."""
    uuid: str
    ssh_port: int
    fingerprint: str
    password_file: pathlib.Path
    vboxmanage: str


class Session:
    """ssh and scp to the guest with its enrolled host key and the guest password through askpass."""

    def __init__(self, guest: Guest, root: pathlib.Path, run: Run) -> None:
        self.run = run
        self.password_file = guest.password_file
        self.known_hosts = root / "known-hosts"
        askpass = root / "askpass.sh"
        askpass.write_text(f"#!/bin/sh\ncat {shlex.quote(str(guest.password_file))}\n", encoding="utf-8")
        askpass.chmod(0o700)
        self.environment = dict(os.environ, DISPLAY=":0", SSH_ASKPASS=str(askpass), SSH_ASKPASS_REQUIRE="force")
        options = ["-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={self.known_hosts}",
                   "-o", "ConnectTimeout=10"]
        self.ssh_argv = ["ssh", "-p", str(guest.ssh_port), *options, f"{GUEST_USER}@{GUEST_HOST}"]
        self.scp_argv = ["scp", "-P", str(guest.ssh_port), *options]

    def ssh(self, command: list[str] | str, timeout: int, stdin=subprocess.DEVNULL) -> tuple[int, str]:
        text = command if isinstance(command, str) else shlex.join(command)
        return self.run([*self.ssh_argv, text], timeout=timeout, environment=self.environment, stdin=stdin)

    def scp(self, source: str, target: str, timeout: int) -> int:
        return self.run([*self.scp_argv, source, target], timeout=timeout, environment=self.environment)[0]

    def sudo(self, command: list[str], timeout: int) -> tuple[int, str]:
        """Run ``command`` as root; sudo reads the guest password from standard input."""
        with self.password_file.open("r", encoding="utf-8") as password:
            return self.ssh(f'sudo -S -p "" {shlex.join(command)}', timeout, stdin=password)


def enroll(guest: Guest, session: Session, run: Run, sleep: Callable[[float], None],
           enroll_script: pathlib.Path = ENROLL_SCRIPT) -> None:
    """Record the guest's host key once it matches ``guest.fingerprint``, retrying while the guest boots."""
    for _ in range(ENROLL_ATTEMPTS):
        session.known_hosts.unlink(missing_ok=True)
        code, _ = run([str(enroll_script), "--host", GUEST_HOST, "--port", str(guest.ssh_port),
                       "--expected-fingerprint", guest.fingerprint, "--out", str(session.known_hosts)],
                      timeout=ENROLL_TIMEOUT)
        if code == 0:
            return
        sleep(ENROLL_INTERVAL)
    raise GuestError("guest SSH identity did not become ready")


def list_runner_units(session: Session) -> list[str]:
    return runner_units([session.ssh(list(query), QUERY_TIMEOUT) for query in RUNNER_UNIT_QUERIES])


def _boot_id(session: Session) -> str:
    code, output = session.ssh(["cat", f"/{BOOT_ID_PATH}"], QUERY_TIMEOUT)
    if code != 0:
        raise GuestError("cannot read the guest boot ID")
    return canonical_uuid(output.strip(), "guest boot ID")


def scan_runner(session: Session) -> dict:
    """The guest's Actions runner units and runner configurations, found in one boot.

    Returns the receipt's ``runnerScan`` block. Raises ``LaneBoxError`` if any part of the scan fails; a finding is
    returned, not raised.
    """
    boot = _boot_id(session)
    units = list_runner_units(session)
    code, output = session.sudo(list(RUNNER_CONFIGURATION_QUERY), SCAN_TIMEOUT)
    if code != 0:
        raise GuestError(f"runner configuration scan failed with exit code {code}")
    configurations = sorted({line for line in output.splitlines() if line.strip()})
    if _boot_id(session) != boot:
        raise GuestError("the guest restarted during the runner scan")
    return {
        "bootId": boot,
        "runnerUnits": units,
        "runnerConfigurations": configurations,
        "skippedRoots": ["/" + name for name in SKIPPED_ROOTS],
        "scannedAt": datetime.now(timezone.utc).isoformat(),
    }


def await_poweroff(guest: Guest, run: Run, sleep: Callable[[float], None]) -> bool:
    for _ in range(POWEROFF_POLLS):
        _, output = run([guest.vboxmanage, "showvminfo", guest.uuid, "--machinereadable"], timeout=QUERY_TIMEOUT)
        if parse_vm_info(output).get("VMState") == "poweroff":
            return True
        sleep(POWEROFF_INTERVAL)
    return False


def press_power_button(guest: Guest, run: Run, sleep: Callable[[float], None]) -> bool:
    run([guest.vboxmanage, "controlvm", guest.uuid, "acpipowerbutton"], timeout=QUERY_TIMEOUT)
    return await_poweroff(guest, run, sleep)


def power_off(guest: Guest, session: Session, run: Run, sleep: Callable[[float], None]) -> bool:
    """Shut the guest down from inside, then with the ACPI power button. True once VirtualBox reports poweroff."""
    session.sudo(["systemctl", "poweroff"], POWEROFF_TIMEOUT)
    return await_poweroff(guest, run, sleep) or press_power_button(guest, run, sleep)
