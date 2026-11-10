"""Guest-side readiness scan (``projectbuddy/ubuntu-lane-readiness@2``).

Run as root inside the booted lane base VM, after ``configure-access``. The scan fails closed: an
unreadable directory, an unreadable dpkg database, or a missing identity file is a failure, not a pass.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
from datetime import datetime, timezone

from .access import access_failures, scan_access
from .common import LaneBoxError, canonical_uuid, hash_line, write_private
from .profile import FORBIDDEN_COMPONENT, Profile
from .toolchain import Runner, evaluate_probe

READINESS_SCHEMA = "projectbuddy/ubuntu-lane-readiness@2"
READINESS_FILENAME = "lane-readiness-receipt.json"
SKIPPED_ROOTS = ("dev", "proc", "run", "sys")
LABVIEW_PATHS = ("etc/natinst", "opt/natinst", "usr/local/natinst")
RUNNER_FILES = frozenset({".runner", ".credentials"})
RUNNER_UNIT = re.compile(r"actions\.runner\..+")
STATE_PATH = "var/lib/projectbuddy/base-bootstrap-state.json"
BOOT_ID_PATH = "proc/sys/kernel/random/boot_id"
DPKG_STATUS = "var/lib/dpkg/status"
NOBLE = {"distribution": "ubuntu", "version": "24.04", "codename": "noble"}
ABSENT_STATES = frozenset({"not-installed", "config-files"})


def _display(root: pathlib.Path, path: pathlib.Path | str) -> str:
    relative = pathlib.Path(path).relative_to(root)
    return "/" + relative.as_posix() if relative.parts else "/"


def scan_runner_absence(root: pathlib.Path) -> dict:
    configurations: list[str] = []
    units: list[str] = []
    errors: list[str] = []

    def on_error(error: OSError) -> None:
        errors.append(f"{_display(root, error.filename)}: {error.strerror}")

    for directory, dirnames, filenames in os.walk(root, onerror=on_error, followlinks=False):
        current = pathlib.Path(directory)
        if current == root:
            dirnames[:] = [name for name in dirnames if name not in SKIPPED_ROOTS]
        dirnames.sort()
        names = set(dirnames) | set(filenames)
        if names & RUNNER_FILES:
            configurations.append(_display(root, current))
        units.extend(_display(root, current / name) for name in sorted(names) if RUNNER_UNIT.fullmatch(name))
    return {
        "runnerConfigurations": configurations,
        "runnerUnits": units,
        "skippedRoots": ["/" + name for name in SKIPPED_ROOTS],
        "scanErrors": errors,
    }


def installed_packages(status_text: str) -> list[str]:
    """Return the packages dpkg records as present (anything but not-installed or config-files)."""
    present: list[str] = []
    for stanza in status_text.split("\n\n"):
        fields: dict[str, str] = {}
        for line in stanza.splitlines():
            if line[:1] in (" ", "\t") or ":" not in line:
                continue
            key, value = line.split(":", 1)
            fields[key] = value.strip()
        status = fields.get("Status", "").split()
        if "Package" in fields and (not status or status[-1] not in ABSENT_STATES):
            present.append(fields["Package"])
    return present


def scan_labview_absence(root: pathlib.Path) -> dict:
    paths = ["/" + path for path in LABVIEW_PATHS if (root / path).exists() or (root / path).is_symlink()]
    errors: list[str] = []
    packages: list[str] = []
    try:
        status_text = (root / DPKG_STATUS).read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        errors.append(f"/{DPKG_STATUS}: {error.strerror}")
    else:
        packages = sorted(name for name in installed_packages(status_text) if FORBIDDEN_COMPONENT.search(name))
    return {"paths": paths, "packages": packages, "scanErrors": errors}


def read_identity(root: pathlib.Path) -> tuple[dict, str]:
    try:
        state = json.loads((root / STATE_PATH).read_text(encoding="utf-8"))
        boot_id = (root / BOOT_ID_PATH).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LaneBoxError(f"guest identity is unreadable: {error}") from error
    vm = state.get("vm") if isinstance(state, dict) else None
    if not isinstance(vm, dict) or not isinstance(vm.get("name"), str) or not vm["name"]:
        raise LaneBoxError("base bootstrap state has no VM name")
    if state.get("os") != NOBLE:
        raise LaneBoxError("base bootstrap state is not ubuntu 24.04 (noble)")
    return {"name": vm["name"], "uuid": canonical_uuid(vm.get("uuid"), "VM UUID")}, canonical_uuid(boot_id, "boot id")


def build_readiness(root: pathlib.Path, profile: Profile, run: Runner) -> dict:
    failures: list[str] = []
    try:
        vm, boot_id = read_identity(root)
    except LaneBoxError as error:
        failures.append(str(error))
        vm, boot_id = None, None
    runner = scan_runner_absence(root)
    labview = scan_labview_absence(root)
    toolchain = [evaluate_probe(tool, run) for tool in profile.tools]
    access = scan_access(root, run)
    if runner["runnerConfigurations"]:
        failures.append("Actions runner configuration is present: " + ", ".join(runner["runnerConfigurations"]))
    if runner["runnerUnits"]:
        failures.append("Actions runner unit is present: " + ", ".join(runner["runnerUnits"]))
    if runner["scanErrors"] or labview["scanErrors"]:
        failures.append("the filesystem scan was incomplete")
    if labview["paths"] or labview["packages"]:
        failures.append("LabVIEW, VIPM, or NI residue is present: " + ", ".join(labview["paths"] + labview["packages"]))
    failures.extend(f"toolchain probe failed: {result['name']}" for result in toolchain if not result["ok"])
    failures.extend(access_failures(access))
    return {
        "schema": READINESS_SCHEMA,
        "profile": {"name": profile.name, "sha256": profile.sha256},
        "vm": vm,
        "bootId": boot_id,
        "runnerAbsence": runner,
        "labviewAbsence": labview,
        "toolchain": toolchain,
        "laneAccess": access,
        "checkedAt": datetime.now(timezone.utc).isoformat(),
        "failures": failures,
        "outcome": "FAIL" if failures else "PASS",
    }


def write_readiness(receipt: dict, output_dir: pathlib.Path) -> pathlib.Path:
    if not output_dir.is_absolute() or not output_dir.is_dir():
        raise LaneBoxError(f"output directory must be an existing absolute path: {output_dir}")
    path = output_dir / READINESS_FILENAME
    write_private(path, json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    write_private(output_dir / f"{READINESS_FILENAME}.sha256", hash_line(path))
    return path
