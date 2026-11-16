"""The lane's SSH channel to its instance (VITechnologies/projectbuddy#206): the run's key and root commands.

``client-key`` generates the run's ed25519 key pair in the run directory before the box is added. The
generated Vagrantfile sets the public key as the host-reserved guest property ``ACCESS_PROPERTY`` before the
instance first boots, and the box's lane-access service makes it root's only authorized key. ``connect`` then
accepts only the host key whose fingerprint the box receipt records, and logs in only with the run's key.
Every clone of a box has the box's host key, but only the instance whose property holds this run's public key
accepts this run's key, and only the host can set that property, so a session reaches this instance and no
other. Commands run through ``run`` without a shell on the host; the guest's root shell reads each one as a
single ``shlex.join`` command line.
"""
from __future__ import annotations

import base64
import hashlib
import os
import pathlib
import re
import shlex
import stat
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

from lane_box.common import LaneBoxError, require_private_directory
from lane_box.receipt import FINGERPRINT_PATTERN
from vagrant_clone.core import PORT_RANGE, RUNNER_UNIT_QUERIES, runner_units
from vagrant_clone.guest import (
    ENROLL_ATTEMPTS, ENROLL_INTERVAL, ENROLL_SCRIPT, ENROLL_TIMEOUT, GUEST_HOST, QUERY_TIMEOUT, run_command,
)

from .common import INSTANCE_NAME

# Read by the box's lane-access service (scripts/lane_box/guest/lane_access.py). The guest cannot set a
# /VirtualBox/HostInfo/ property, and a host set makes it read-only to the guest.
ACCESS_PROPERTY = "/VirtualBox/HostInfo/ProjectBuddy/Lane/ClientKey"
CLIENT_KEY = "client-key"
PUBLIC_KEY = f"{CLIENT_KEY}.pub"
KNOWN_HOSTS = "known_hosts"
ACCOUNT = "root"
KEY_ALGORITHM = "ssh-ed25519"
KEY_DATA = r"[A-Za-z0-9+/]{68}"
BLOB_PREFIX = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20"
# ssh expands % tokens and a leading ~ in IdentityFile and UserKnownHostsFile, and splits an -o value at
# whitespace and quotes, so a run directory is accepted only in this form.
SSH_PATH = re.compile(r"/[A-Za-z0-9._/@+-]*")
KEYGEN_TIMEOUT = 60
# The guest's lane-access service runs before ssh.service; these retries cover socket activation.
AUTH_ATTEMPTS, AUTH_INTERVAL = 12, 5
SSH_FAILED = 255
# IdentityFile is an option, not -i: ssh drops an -i file it cannot read and offers ~/.ssh's default keys.
SSH_OPTIONS = (
    "BatchMode=yes", "IdentitiesOnly=yes", "IdentityAgent=none", "StrictHostKeyChecking=yes",
    "GlobalKnownHostsFile=/dev/null", "HostKeyAlgorithms=ssh-ed25519", "UpdateHostKeys=no", "CheckHostIP=no",
    "PasswordAuthentication=no", "KbdInteractiveAuthentication=no", "ControlMaster=no", "ControlPath=none",
    "ForwardAgent=no", "ForwardX11=no", "ClearAllForwardings=yes", "RequestTTY=no", "ConnectTimeout=10",
)

Run = Callable[..., "tuple[int, str]"]


def ssh_path(path: pathlib.Path) -> str:
    text = str(path)
    if not SSH_PATH.fullmatch(text):
        raise LaneBoxError(f"ssh would expand or split the path {text!r}; it must match {SSH_PATH.pattern}")
    return text


def fingerprint(blob: bytes) -> str:
    """OpenSSH's SHA-256 fingerprint of a public key blob."""
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")


def key_blob(data: str) -> bytes:
    """The blob of an ed25519 public key from its base64 text; anything else is refused."""
    if not re.fullmatch(KEY_DATA, data):
        raise LaneBoxError("not an ed25519 public key")
    blob = base64.b64decode(data, validate=True)
    if not blob.startswith(BLOB_PREFIX):
        raise LaneBoxError("not an ed25519 public key")
    return blob


def _read_regular(path: pathlib.Path, private: bool) -> str:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise LaneBoxError(f"cannot open {path.name}: {error}") from error
    with os.fdopen(descriptor, "rb") as stream:
        status = os.fstat(stream.fileno())
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1 or status.st_uid != os.geteuid():
            raise LaneBoxError(f"{path.name} is not a regular file with one link that this account owns")
        if private and status.st_mode & 0o077:
            raise LaneBoxError(f"{path.name} gives its group or others access")
        try:
            return stream.read(4096).decode("ascii")
        except UnicodeDecodeError as error:
            raise LaneBoxError(f"{path.name} is not ASCII") from error


def generate_client_key(run_dir: pathlib.Path, instance: str, run: Run = run_command) -> dict:
    """``client-key``: a new ed25519 key pair without a passphrase, commented with the instance's name.

    Refuses when either file already exists. Returns the public key's algorithm and fingerprint.
    """
    if not INSTANCE_NAME.fullmatch(instance):
        raise LaneBoxError(f"{instance!r} is not a lane instance name")
    require_private_directory(run_dir)
    private, public = run_dir / CLIENT_KEY, run_dir / PUBLIC_KEY
    if os.path.lexists(private) or os.path.lexists(public):
        raise LaneBoxError("the run directory already has a client key")
    code, _ = run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", instance, "-f", ssh_path(private)],
                  timeout=KEYGEN_TIMEOUT)
    if code != 0:
        raise LaneBoxError(f"ssh-keygen failed with exit code {code}")
    _read_regular(private, private=True)
    match = re.fullmatch(rf"{KEY_ALGORITHM} ({KEY_DATA}) {re.escape(instance)}\n", _read_regular(public, private=False))
    if not match:
        raise LaneBoxError(f"{PUBLIC_KEY} is not one ed25519 public key commented with the instance's name")
    return {"algorithm": KEY_ALGORITHM, "fingerprint": fingerprint(key_blob(match.group(1)))}


@dataclass(frozen=True)
class Guest:
    """The instance as its plan names it: the run directory, the loopback SSH port, and the box's host key."""
    run_dir: pathlib.Path
    port: int
    fingerprint: str

    @classmethod
    def from_plan(cls, plan: Mapping) -> "Guest":
        """Only a ``ready`` plan names an instance to connect to."""
        try:
            run_dir, port = plan["instance"]["runDirectory"], plan["network"]["sshForward"]["hostPort"]
            key = plan["box"]["sshHostKey"]
            algorithm, expected = key["algorithm"], key["fingerprint"]
        except (KeyError, TypeError) as error:
            raise LaneBoxError("the plan does not name the instance's run directory, SSH port, and host key") from error
        if plan.get("outcome") != "ready":
            raise LaneBoxError("only a ready plan names an instance to connect to")
        if not isinstance(run_dir, str) or type(port) is not int or port not in PORT_RANGE:
            raise LaneBoxError("the plan's run directory or SSH port is invalid")
        if algorithm != KEY_ALGORITHM or not isinstance(expected, str) or not FINGERPRINT_PATTERN.fullmatch(expected):
            raise LaneBoxError("the plan's SSH host key is not an ed25519 fingerprint")
        return cls(pathlib.Path(run_dir), port, expected)


class Session:
    """Commands run as root in the instance over its loopback SSH forward, with the run's key."""

    def __init__(self, guest: Guest, run: Run = run_command) -> None:
        self.run = run
        self.argv = [
            "ssh", "-F", "none", "-p", str(guest.port),
            "-o", f"IdentityFile={ssh_path(guest.run_dir / CLIENT_KEY)}",
            "-o", f"UserKnownHostsFile={ssh_path(guest.run_dir / KNOWN_HOSTS)}",
            *(word for option in SSH_OPTIONS for word in ("-o", option)),
            # ssh reads options after the destination too; after "--" the command cannot be one.
            "--", f"{ACCOUNT}@{GUEST_HOST}",
        ]

    def command(self, argv: Sequence[str], timeout: int, stdin=subprocess.DEVNULL) -> tuple[int, str]:
        if not argv or not all(isinstance(word, str) and word for word in argv):
            raise LaneBoxError("a guest command must be a non-empty list of non-empty strings")
        return self.run([*self.argv, shlex.join(argv)], timeout=timeout, stdin=stdin)


def check_known_hosts(guest: Guest) -> None:
    """The enrolled file holds one key, for the instance's port, whose fingerprint is the box receipt's."""
    text = _read_regular(guest.run_dir / KNOWN_HOSTS, private=True)
    match = re.fullmatch(rf"\[{re.escape(GUEST_HOST)}\]:{guest.port} {KEY_ALGORITHM} ({KEY_DATA})\n", text)
    if not match or fingerprint(key_blob(match.group(1))) != guest.fingerprint:
        raise LaneBoxError(f"{KNOWN_HOSTS} is not the box receipt's host key for the instance's port")


def enroll(guest: Guest, run: Run = run_command, sleep: Callable[[float], None] = time.sleep) -> None:
    """Record the instance's host key once it matches the box receipt, retrying while the instance boots."""
    known_hosts = guest.run_dir / KNOWN_HOSTS
    if os.path.lexists(known_hosts):
        raise LaneBoxError(f"the run directory already has a {KNOWN_HOSTS} file")
    for _ in range(ENROLL_ATTEMPTS):
        code, _ = run([str(ENROLL_SCRIPT), "--host", GUEST_HOST, "--port", str(guest.port),
                       "--expected-fingerprint", guest.fingerprint, "--out", ssh_path(known_hosts)],
                      timeout=ENROLL_TIMEOUT)
        if code == 0:
            check_known_hosts(guest)
            return
        sleep(ENROLL_INTERVAL)
    raise LaneBoxError("the instance's SSH host key did not become ready or does not match the box receipt")


def connect(guest: Guest, run: Run = run_command, sleep: Callable[[float], None] = time.sleep) -> Session:
    """``connect``: enroll the host key, then log in as root with the run's key."""
    enroll(guest, run, sleep)
    session = Session(guest, run)
    for _ in range(AUTH_ATTEMPTS):
        code, output = session.command(["id", "-u"], QUERY_TIMEOUT)
        if code != SSH_FAILED:
            break
        sleep(AUTH_INTERVAL)
    if code != 0:
        raise LaneBoxError(f"cannot log in to the instance as {ACCOUNT} with the run's key (exit code {code})")
    if output != "0\n":
        raise LaneBoxError(f"the instance's {ACCOUNT} login is not user ID 0")
    return session


def check_runner_units(session: Session) -> None:
    """``check-runner-units``: an instance with any Actions runner unit stops the run (REQ-103)."""
    units = runner_units([session.command(list(query), QUERY_TIMEOUT) for query in RUNNER_UNIT_QUERIES])
    if units:
        raise LaneBoxError(f"the instance has Actions runner units: {', '.join(units)}")
