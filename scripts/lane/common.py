"""Lane instance names, VM UUID targets, and the Vagrant machine ID that binds a run directory."""
from __future__ import annotations

import hashlib
import os
import pathlib
import re
import secrets
import tempfile
from typing import NamedTuple

from lane_box.common import LaneBoxError

# A lane instance is named "pb-lane-" plus a random suffix when its plan is made. A name derived
# from the inputs would collide when the same inputs are planned again.
INSTANCE_NAME = re.compile(r"pb-lane-[0-9a-f]{12}")
UUID_PATTERN = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
# Where Vagrant records the VirtualBox UUID of a run directory's single machine (Vagrant 2.4.9,
# lib/vagrant/environment.rb and lib/vagrant/machine.rb).
MACHINE_ID = pathlib.Path(".vagrant", "machines", "default", "virtualbox", "id")


class Refusal(NamedTuple):
    """One failed plan check: the rule's name and what failed."""

    rule: str
    detail: str


def new_instance_name() -> str:
    return f"pb-lane-{secrets.token_hex(6)}"


def write_new(path: pathlib.Path, content: str) -> None:
    """Write ``content`` with mode 600 as a new file, and never replace an existing one.

    The file is written under a temporary name and hard-linked into place. A link fails when the name
    exists, so a file that appeared after the caller checked for it is kept and this write fails.
    """
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        os.unlink(temporary)


def write_new_with_digest(path: pathlib.Path, hash_path: pathlib.Path, content: str) -> None:
    """Write ``content`` and then its ``sha256sum``-style line, both as new files.

    The digest is of ``content`` itself, not a reread of ``path``."""
    write_new(path, content)
    write_new(hash_path, f"{hashlib.sha256(content.encode('utf-8')).hexdigest()}  {path.name}\n")


def target_uuid(value: str) -> str:
    """Accept only a VM UUID in its 8-4-4-4-12 form; a VM name or any other form is refused."""
    if not UUID_PATTERN.fullmatch(value):
        raise LaneBoxError(f"lane targets are VM UUIDs only; refused target: {value!r}")
    return value.lower()
