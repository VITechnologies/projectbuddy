"""Neutral clone checks: the VM's identity, its loopback SSH forward, and its Actions runner units.

None of these checks knows what the clone is for. A clone's purpose is its profile (``profile``).
"""
from __future__ import annotations

import pathlib
import re
from typing import Mapping, Sequence

from lane_box.common import VM_NAME_PATTERN, LaneBoxError, canonical_uuid, require_private_directory
from lane_box.readiness import RUNNER_UNIT

SSH_GUEST_PORT = "22"
PORT_RANGE = range(1024, 65536)
# Listed without a pattern: with a pattern that matches nothing, list-unit-files exits 1 and prints
# nothing, which is indistinguishable from a failure. A booted guest always lists some unit.
RUNNER_UNIT_QUERIES = (
    ("systemctl", "list-unit-files", "--no-pager", "--no-legend", "--plain", "--full"),
    ("systemctl", "list-units", "--all", "--no-pager", "--no-legend", "--plain", "--full"),
)
UNIT_NAME = re.compile(r"\S+\.[a-z]+")
# systemd omits the state glyph under --plain; one is tolerated in case a version prints it anyway.
STATE_GLYPHS = frozenset({"\u25cf", "\u25cb", "\u00d7", "*"})


class CloneError(LaneBoxError):
    pass


def check_request(name: str, uuid: str, ssh_port: int, password_file: pathlib.Path,
                  evidence_dir: pathlib.Path) -> str:
    """Check what the caller supplied before anything runs. Returns the canonical clone UUID."""
    canonical = canonical_uuid(uuid, "clone VM UUID")
    if not VM_NAME_PATTERN.fullmatch(name):
        raise CloneError("VM name is invalid")
    if type(ssh_port) is not int or ssh_port not in PORT_RANGE:
        raise CloneError("SSH port must be from 1024 through 65535")
    if not password_file.is_absolute() or not password_file.is_file():
        raise CloneError("guest password file must be an existing absolute path")
    if password_file.stat().st_mode & 0o077:
        raise CloneError("guest password file must have mode 600 or stricter")
    try:
        require_private_directory(evidence_dir)
    except LaneBoxError as error:
        raise CloneError(f"evidence {error}") from error
    return canonical


def check_machine_id(path: pathlib.Path, uuid: str) -> None:
    """Vagrant's machine ID file must name the clone's UUID."""
    if not path.is_absolute() or not path.is_file():
        raise CloneError("Vagrant machine ID file must be an existing absolute path")
    try:
        recorded = canonical_uuid(path.read_text(encoding="utf-8").strip(), "Vagrant machine ID")
    except (OSError, UnicodeDecodeError) as error:
        raise CloneError(f"Vagrant machine ID file is unreadable: {error}") from error
    if recorded != uuid:
        raise CloneError("Vagrant machine ID does not match the expected clone UUID")


def parse_vm_info(output: str) -> dict[str, str]:
    values = {}
    for line in output.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value.strip().strip('"')
    return values


def check_instance(info: Mapping[str, str], name: str, uuid: str, ssh_port: int) -> None:
    """``showvminfo <uuid> --machinereadable``: the name, the UUID, ``running``, and one loopback SSH forward."""
    if info.get("UUID") != uuid:
        raise CloneError("clone VM UUID does not match expected identity")
    if info.get("name") != name:
        raise CloneError("clone VM name does not match expected identity")
    if info.get("VMState") != "running":
        raise CloneError("clone VM must be running")
    rules = [value.split(",") for key, value in info.items() if key.startswith("Forwarding(")]
    guest_ssh = [rule for rule in rules if len(rule) == 6 and rule[5] == SSH_GUEST_PORT]
    expected = [rule for rule in guest_ssh if rule[1:] == ["tcp", "127.0.0.1", str(ssh_port), "", SSH_GUEST_PORT]]
    if len(expected) != 1 or guest_ssh != expected:
        raise CloneError("clone does not have exactly one loopback SSH forwarding rule for the expected port")


def runner_units(results: Sequence[tuple[int, str]]) -> list[str]:
    """The ``actions.runner.*`` units that the ``RUNNER_UNIT_QUERIES`` commands list, sorted and once each.

    A failed query, a query that lists nothing, or a line that does not start with a unit name fails.
    """
    if len(results) != len(RUNNER_UNIT_QUERIES):
        raise CloneError("every runner unit query must have a result")
    found: set[str] = set()
    for query, (code, output) in zip(RUNNER_UNIT_QUERIES, results):
        lines = [line.split() for line in output.splitlines() if line.strip()]
        if code != 0:
            raise CloneError(f"systemctl {query[1]} failed with exit code {code}")
        if not lines:
            raise CloneError(f"systemctl {query[1]} listed no units")
        for fields in lines:
            if fields[0] in STATE_GLYPHS:
                fields = fields[1:]
            if not fields or not UNIT_NAME.fullmatch(fields[0]):
                raise CloneError(f"systemctl {query[1]} printed a line that is not a unit")
            if RUNNER_UNIT.fullmatch(fields[0]):
                found.add(fields[0])
    return sorted(found)
