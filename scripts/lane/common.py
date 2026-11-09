"""Lane instance names, VM UUID targets, and the Vagrant machine ID that binds a run directory."""
from __future__ import annotations

import pathlib
import re

from lane_box.common import LaneBoxError

# A lane instance is named "pb-lane-" plus a random suffix when its run starts.
INSTANCE_NAME = re.compile(r"pb-lane-[0-9a-f]{12}")
UUID_PATTERN = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
# Where Vagrant records the VirtualBox UUID of a run directory's single machine (Vagrant 2.4.9,
# lib/vagrant/environment.rb and lib/vagrant/machine.rb).
MACHINE_ID = pathlib.Path(".vagrant", "machines", "default", "virtualbox", "id")


def target_uuid(value: str) -> str:
    """Accept only a VM UUID in its 8-4-4-4-12 form; a VM name or any other form is refused."""
    if not UUID_PATTERN.fullmatch(value):
        raise LaneBoxError(f"lane targets are VM UUIDs only; refused target: {value!r}")
    return value.lower()
