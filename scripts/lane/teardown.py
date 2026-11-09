"""Lane teardown (REQ-102): change only the run's verified UUID, and only from an allow-listed state.

``decide`` is the allow-list. ``teardown`` applies it under a host lock keyed by the UUID, deletes
the VM by UUID with ``VBoxManage unregistervm --delete``, removes the run directory, re-hashes the
box, and writes a ``projectbuddy/lane-teardown@1`` receipt. A receipt claims that the VM is gone
only after ``VBoxManage list vms`` no longer shows the UUID; ``check_receipt`` rejects any other
claim. ``plan`` is the dry run: it observes the VM and changes nothing.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import pathlib
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Iterator

from lane_box.common import (
    SHA256_PATTERN, LaneBoxError, forbidden_keys, hash_line, require_private_directory, sha256_file,
    verify_hash_file, write_private,
)
from lane_box.packager import parse_vm_info
from lane_box.receipt import load_json

from .common import INSTANCE_NAME, MACHINE_ID, UUID_PATTERN, target_uuid

TEARDOWN_SCHEMA = "projectbuddy/lane-teardown@1"
ABSENT = "absent"
POWEROFF_FROM = frozenset({"running", "paused"})
DELETE_FROM = frozenset({"poweroff", "aborted"})
# The states a VM may report while a poweroff takes effect; any other state ends the wait.
STOPPING = frozenset({"running", "paused", "stopping"})
QUERY_TIMEOUT = 120
POWEROFF_TIMEOUT = 300
POWEROFF_WAIT = 120
DELETE_TIMEOUT = 600
# Longer than one teardown can take, so a second request for the same UUID waits for the first.
LOCK_WAIT = 1800
POLL_INTERVAL = 2
OUTCOMES = ("removed", "absent", "refused", "failed")
RUN_DIRECTORY_STATES = ("removed", "absent", "kept")
RECEIPT_FIELDS = frozenset({"schema", "instance", "allowList", "steps", "runDirectory", "box", "failures", "outcome"})


class ReceiptNotWritten(LaneBoxError):
    """The teardown ran, but its receipt could not be written."""

    def __init__(self, message: str, changed: bool) -> None:
        super().__init__(message)
        self.changed = changed


def decide(state: str) -> str | None:
    """The REQ-102 allow-list: the action for an observed state, or None to refuse the state."""
    if state in POWEROFF_FROM:
        return "poweroff"
    if state in DELETE_FROM:
        return "delete"
    return None


def command_for(action: str, uuid: str) -> list[str]:
    if action == "poweroff":
        return ["controlvm", uuid, "poweroff"]
    return ["unregistervm", uuid, "--delete"]


def allow_list() -> dict[str, list[str]]:
    return {"poweroff": sorted(POWEROFF_FROM), "delete": sorted(DELETE_FROM)}


class VirtualBox:
    """VBoxManage, called without a shell, addressing the VM by UUID only."""

    def __init__(self, vboxmanage: str) -> None:
        self.vboxmanage = vboxmanage

    def _call(self, argv: list[str], timeout: float) -> subprocess.CompletedProcess:
        return subprocess.run(
            [self.vboxmanage, *argv], capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL, start_new_session=True, check=False,
        )

    def _query(self, argv: list[str], message: str) -> subprocess.CompletedProcess:
        try:
            return self._call(argv, QUERY_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise LaneBoxError(f"{message}: {error}") from error

    def _registered(self) -> set[str]:
        listing = self._query(["list", "vms"], "cannot list the registered VMs")
        if listing.returncode != 0:
            raise LaneBoxError("cannot list the registered VMs")
        return {value.lower() for value in re.findall(r"\{([0-9a-fA-F-]{36})\}\s*$", listing.stdout, re.MULTILINE)}

    def observe(self, uuid: str) -> tuple[str, str | None]:
        """Return the VM's state and name, or ``("absent", None)`` when ``list vms`` does not show the UUID."""
        if uuid not in self._registered():
            return ABSENT, None
        shown = self._query(["showvminfo", uuid, "--machinereadable"], "cannot read the VM state")
        if shown.returncode != 0:
            # The VM can be unregistered between the two queries.
            if uuid not in self._registered():
                return ABSENT, None
            raise LaneBoxError("cannot read the VM state")
        info = parse_vm_info(shown.stdout)
        if info.get("UUID", "").lower() != uuid:
            raise LaneBoxError("VirtualBox reported a different UUID")
        if not info.get("VMState"):
            raise LaneBoxError("VirtualBox reported no VM state")
        return info["VMState"], info.get("name")

    def run(self, argv: list[str], timeout: float) -> int | str:
        """Run a state-changing command; return its exit code, or ``"timeout"``.

        On a timeout the VBoxManage client is killed; VirtualBox may still finish the job, and the
        next observation shows the result.
        """
        try:
            return self._call(argv, timeout).returncode
        except subprocess.TimeoutExpired:
            return "timeout"
        except OSError as error:
            raise LaneBoxError(f"cannot run VBoxManage: {error}") from error


@dataclass(frozen=True)
class Request:
    uuid: str
    run_dir: pathlib.Path
    box: pathlib.Path
    box_sha256: str
    lock_dir: pathlib.Path
    receipt_out: pathlib.Path

    @property
    def receipt_hash(self) -> pathlib.Path:
        return pathlib.Path(f"{self.receipt_out}.sha256")


def _inside(path: pathlib.Path, directory: pathlib.Path) -> bool:
    return path == directory or directory in path.parents


def leftover_for(run_dir: pathlib.Path, uuid: str) -> pathlib.Path:
    """Where teardown renames the run directory before deleting it, so a rerun can finish an interrupted delete."""
    return run_dir.parent / f".{run_dir.name}.teardown-{uuid}"


def check_request(request: Request) -> str:
    """Validate the inputs before any VirtualBox query; return the canonical UUID."""
    uuid = target_uuid(request.uuid)
    for path, option in ((request.run_dir, "--run-dir"), (request.box, "--box"), (request.receipt_out, "--receipt-out")):
        if not path.is_absolute():
            raise LaneBoxError(f"{option} must be an absolute path")
    if ".." in request.run_dir.parts:
        raise LaneBoxError("--run-dir must not contain '..'")
    if not SHA256_PATTERN.fullmatch(request.box_sha256):
        raise LaneBoxError("--box-sha256 must be 64 lowercase hexadecimal digits")
    if not request.box.is_file():
        raise LaneBoxError(f"box file is missing: {request.box}")
    require_private_directory(request.lock_dir)
    require_private_directory(request.receipt_out.parent)
    if os.path.lexists(request.receipt_out) or os.path.lexists(request.receipt_hash):
        raise LaneBoxError(f"receipt output already exists: {request.receipt_out}")
    deleted = (request.run_dir.resolve(), leftover_for(request.run_dir, uuid).resolve())
    for path, what in ((request.box, "box file"), (request.receipt_out, "receipt"), (request.lock_dir, "lock directory")):
        if any(_inside(path.resolve(), directory) for directory in deleted):
            raise LaneBoxError(f"the {what} must be outside the run directory, which teardown deletes")
    return uuid


def bind_run_directory(run_dir: pathlib.Path, uuid: str) -> bool:
    """Return False when the run directory is gone; otherwise require Vagrant's machine ID to be ``uuid``."""
    if not os.path.lexists(run_dir):
        return False
    if run_dir.is_symlink() or not run_dir.is_dir():
        raise LaneBoxError("the run directory is not a directory")
    require_private_directory(run_dir)
    current = run_dir
    for part in MACHINE_ID.parts:
        current = current / part
        if current.is_symlink():
            raise LaneBoxError(f"the run directory's Vagrant machine ID path has a symbolic link: {part}")
    if not current.is_file():
        raise LaneBoxError("the run directory has no Vagrant machine ID, so teardown cannot bind it to the UUID")
    try:
        recorded = current.read_text(encoding="utf-8").strip().lower()
    except (OSError, UnicodeDecodeError) as error:
        raise LaneBoxError(f"cannot read the run directory's Vagrant machine ID: {error}") from error
    if recorded != uuid:
        raise LaneBoxError("the run directory's Vagrant machine ID names a different VM")
    return True


@contextlib.contextmanager
def uuid_lock(lock_dir: pathlib.Path, uuid: str) -> Iterator[None]:
    """Hold an exclusive lock on ``<lock_dir>/<uuid>.lock``; wait up to ``LOCK_WAIT`` seconds for it."""
    try:
        descriptor = os.open(lock_dir / f"{uuid}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as error:
        raise LaneBoxError(f"cannot open the teardown lock: {error}") from error
    try:
        deadline = time.monotonic() + LOCK_WAIT
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LaneBoxError("another teardown of this UUID still holds the lock") from None
                time.sleep(POLL_INTERVAL)
        yield
    finally:
        os.close(descriptor)


def refusal(state: str, name: str | None, bound: bool) -> str | None:
    """The rule that refuses to touch a present VM, or None when teardown may proceed."""
    if state == ABSENT:
        return None
    if not bound:
        return "no run directory binds this UUID to a lane run; the VM is left in place"
    if not INSTANCE_NAME.fullmatch(name or ""):
        return f"VM {name!r} is not a lane instance; the VM is left in place"
    if decide(state) is None:
        return f"state {state!r} is outside the teardown allow-list; the VM is left in place"
    return None


@dataclass
class Record:
    """What one teardown observed and did, in order."""

    uuid: str
    name: str | None = None
    steps: list[dict] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    refused: bool = False
    deleted: bool = False
    changed: bool = False
    run_directory: str = "kept"
    box_after: str | None = None

    def observe(self, vbox: VirtualBox) -> str:
        state, name = vbox.observe(self.uuid)
        if self.name is None:
            self.name = name
        if not self.steps or self.steps[-1] != {"observed": state}:
            self.steps.append({"observed": state})
        return state

    def command(self, vbox: VirtualBox, action: str, timeout: float) -> None:
        argv = command_for(action, self.uuid)
        # Counted before the call: a command that fails or times out may still have changed the VM.
        self.changed = True
        result = vbox.run(argv, timeout)
        self.steps.append({"command": argv, "result": result})
        if result != 0:
            detail = "timed out" if result == "timeout" else f"exit code {result}"
            raise LaneBoxError(f"VBoxManage {argv[0]} failed: {detail}; the VM may be left in place")
        self.deleted = self.deleted or action == "delete"

    def refuse(self, rule: str) -> None:
        self.refused = True
        self.failures.append(rule)

    def receipt(self, expected_box_sha256: str) -> dict:
        if self.failures:
            outcome = "refused" if self.refused else "failed"
        else:
            outcome = "removed" if self.deleted else "absent"
        return {
            "schema": TEARDOWN_SCHEMA,
            "instance": {"uuid": self.uuid, "name": self.name},
            "allowList": allow_list(),
            "steps": self.steps,
            "runDirectory": self.run_directory,
            "box": {"expectedSha256": expected_box_sha256, "sha256AfterTeardown": self.box_after},
            "failures": self.failures,
            "outcome": outcome,
        }


def plan(request: Request, vbox: VirtualBox) -> list[str]:
    """The dry run: validate, bind, and observe; print the commands teardown would run."""
    uuid = check_request(request)
    bound = bind_run_directory(request.run_dir, uuid)
    state, name = vbox.observe(uuid)
    lines = [
        "Lane teardown: PLAN", f"uuid={uuid}", f"name={name or ''}", f"observed={state}",
        f"run_directory={'bound' if bound else 'absent'}",
    ]
    rule = refusal(state, name, bound)
    if rule:
        raise LaneBoxError(rule)
    actions = (["poweroff"] if state in POWEROFF_FROM else []) + ([] if state == ABSENT else ["delete"])
    lines += [f"step=VBoxManage {' '.join(command_for(action, uuid))}" for action in actions]
    leftover = leftover_for(request.run_dir, uuid)
    if os.path.lexists(leftover):
        lines.append(f"step=remove {leftover}")
    if bound:
        lines.append(f"step=remove {request.run_dir}")
    lines.append(f"step=check the box SHA-256 is {request.box_sha256}")
    return lines


def _wait_for_stop(record: Record, vbox: VirtualBox) -> str:
    deadline = time.monotonic() + POWEROFF_WAIT
    while True:
        state = record.observe(vbox)
        if state not in STOPPING or time.monotonic() >= deadline:
            return state
        time.sleep(POLL_INTERVAL)


def _finish(record: Record, request: Request, bound: bool) -> None:
    # The rename is atomic, so an interrupted delete leaves either the bound run directory or the
    # leftover; a rerun that observes the UUID absent deletes the leftover.
    leftover = leftover_for(request.run_dir, record.uuid)
    found = os.path.lexists(leftover)
    if found and (leftover.is_symlink() or not leftover.is_dir()):
        raise LaneBoxError(f"the leftover run directory is not a directory: {leftover}")
    try:
        if found:
            require_private_directory(leftover)
            shutil.rmtree(leftover)
        if bound:
            os.rename(request.run_dir, leftover)
            shutil.rmtree(leftover)
    except OSError as error:
        raise LaneBoxError(f"cannot remove the run directory: {error}") from error
    record.run_directory = "removed" if bound or found else "absent"
    try:
        record.box_after = sha256_file(request.box)
    except OSError as error:
        raise LaneBoxError(f"cannot hash the box file: {error}") from error
    if record.box_after != request.box_sha256:
        raise LaneBoxError("the box file changed: its SHA-256 differs from the expected digest")


def _apply(record: Record, request: Request, vbox: VirtualBox, bound: bool) -> None:
    state = record.observe(vbox)
    rule = refusal(state, record.name, bound)
    if rule:
        record.refuse(rule)
        return
    if state in POWEROFF_FROM:
        record.command(vbox, "poweroff", POWEROFF_TIMEOUT)
        # An acknowledged poweroff is not an observed one (REQ-055).
        state = _wait_for_stop(record, vbox)
    if state in DELETE_FROM:
        record.command(vbox, "delete", DELETE_TIMEOUT)
        if record.observe(vbox) != ABSENT:
            raise LaneBoxError("the VM is still registered after the delete")
    elif state in STOPPING:
        raise LaneBoxError(f"the VM did not stop within {POWEROFF_WAIT} s; it is left in place")
    elif state != ABSENT:
        record.refuse(f"state {state!r} is outside the teardown allow-list; the VM is left in place")
        return
    _finish(record, request, bound)


def teardown(request: Request, vbox: VirtualBox) -> tuple[dict, bool]:
    """Tear down one lane instance; return the receipt and whether any command was run."""
    uuid = check_request(request)
    with uuid_lock(request.lock_dir, uuid):
        bound = bind_run_directory(request.run_dir, uuid)
        record = Record(uuid)
        try:
            _apply(record, request, vbox, bound)
        except LaneBoxError as error:
            record.failures.append(str(error))
        receipt = record.receipt(request.box_sha256)
        try:
            write_private(request.receipt_out, json.dumps(receipt, indent=2, sort_keys=True) + "\n")
            write_private(request.receipt_hash, hash_line(request.receipt_out))
        except OSError as error:
            raise ReceiptNotWritten(
                f"teardown ended {receipt['outcome']}, but its receipt cannot be written: {error}", record.changed,
            ) from error
    return receipt, record.changed


def _check_steps(steps: object, uuid: str, errors: list[str]) -> tuple[bool, bool, bool]:
    """Return (absence observed, delete succeeded, any command run); append each rule a step breaks."""
    if not isinstance(steps, list):
        errors.append("the steps are not a list")
        return False, False, False
    absent = deleted = commanded = False
    previous: dict = {}
    for index, step in enumerate(steps):
        if absent:
            errors.append(f"step {index} follows the absence observation")
            break
        if isinstance(step, dict) and set(step) == {"observed"} and isinstance(step["observed"], str):
            absent = step["observed"] == ABSENT
        elif isinstance(step, dict) and set(step) == {"command", "result"}:
            commanded = True
            action = next((name for name in ("poweroff", "delete") if step["command"] == command_for(name, uuid)), None)
            if action is None:
                errors.append(f"step {index} is not a poweroff or delete of the instance UUID")
                break
            if "observed" not in previous or decide(previous["observed"]) != action:
                errors.append(f"step {index} runs {action} without an observed state that allows it")
                break
            if not (type(step["result"]) is int or step["result"] == "timeout"):
                errors.append(f"step {index} has a result that is neither an exit code nor \"timeout\"")
                break
            if step["result"] != 0:
                if index != len(steps) - 1:
                    errors.append(f"step {index} failed, but steps follow it")
                break
            deleted = deleted or action == "delete"
        else:
            errors.append(f"step {index} is malformed")
            break
        previous = step
    return absent, deleted, commanded


def check_receipt(receipt: dict, expected_uuid: str | None = None, box_sha256: str | None = None) -> list[str]:
    """Every rule a ``projectbuddy/lane-teardown@1`` receipt breaks; an empty list means it is consistent."""
    errors: list[str] = []

    def require(condition: bool, message: str) -> None:
        if not condition:
            errors.append(message)

    require(set(receipt) == RECEIPT_FIELDS, "the receipt has unexpected or missing fields")
    require(receipt.get("schema") == TEARDOWN_SCHEMA, "unexpected schema")
    require(not forbidden_keys(receipt), "the receipt holds a credential field")
    instance = receipt.get("instance") if isinstance(receipt.get("instance"), dict) else {}
    uuid = instance.get("uuid")
    if not (isinstance(uuid, str) and UUID_PATTERN.fullmatch(uuid) and uuid == uuid.lower()):
        return [*errors, "the instance UUID is not a canonical UUID"]
    require(expected_uuid is None or uuid == expected_uuid, "the instance UUID does not match")
    name = instance.get("name")
    require(name is None or isinstance(name, str), "the instance name is not a string")
    name = name if isinstance(name, str) else None
    require(receipt.get("allowList") == allow_list(), "the allow-list differs from REQ-102")
    absent, deleted, commanded = _check_steps(receipt.get("steps"), uuid, errors)
    require(not commanded or INSTANCE_NAME.fullmatch(name or "") is not None,
            "a command was run against a VM that is not a lane instance")
    box = receipt.get("box") if isinstance(receipt.get("box"), dict) else {}
    expected = box.get("expectedSha256")
    require(isinstance(expected, str) and SHA256_PATTERN.fullmatch(expected) is not None,
            "the expected box digest is not a SHA-256")
    require(box_sha256 is None or expected == box_sha256, "the expected box digest does not match")
    require(receipt.get("runDirectory") in RUN_DIRECTORY_STATES, "unknown run directory state")
    failures = receipt.get("failures")
    require(isinstance(failures, list) and all(isinstance(item, str) for item in failures), "failures is not a list of strings")
    outcome = receipt.get("outcome")
    require(outcome in OUTCOMES, "unknown outcome")
    if outcome in ("removed", "absent"):
        require(failures == [], "a removal claim carries failures")
        require(absent, "the receipt claims removal without observing the UUID absent")
        require(deleted == (outcome == "removed"), "the outcome does not match the delete commands")
        require(receipt.get("runDirectory") in ("removed", "absent"), "the run directory was not removed")
        require(box.get("sha256AfterTeardown") == expected, "the box digest after teardown differs")
    else:
        require(bool(failures), f"a {outcome} receipt names no failure")
    if outcome == "refused":
        steps = receipt.get("steps") if isinstance(receipt.get("steps"), list) else []
        require(bool(steps) and isinstance(steps[-1], dict) and set(steps[-1]) == {"observed"} and not absent,
                "a refusal must end with an observation of a VM that is still present")
    return errors


def verify_receipt(path: pathlib.Path, expected_uuid: str | None = None, box_sha256: str | None = None) -> dict:
    """Check the receipt's hash file and its rules; raise naming every broken rule."""
    receipt = load_json(path, "teardown receipt")
    verify_hash_file(pathlib.Path(f"{path}.sha256"), path)
    errors = check_receipt(receipt, None if expected_uuid is None else target_uuid(expected_uuid), box_sha256)
    if errors:
        raise LaneBoxError("; ".join(errors))
    return receipt
