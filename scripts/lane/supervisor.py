"""The lane's guest supervisor (REQ-106): it runs the fetch and build phases and hands off to the launcher.

The engine places this file beside ``output_disk.py`` in the guest's ``/run/projectbuddy-lane``
directory, with the run's ``lane-guest.json`` spec. It uses the standard library only, so the guest's
``python3`` runs it. Its modes:

``fetch``
    root, over SSH: run the fetch unit, wait until it ends, empty its control group, and print its
    result; exit 0 only when the result is ``exited 0``.
``start-build``
    root, over SSH while the link is on: make every check that does not need carrier loss, then start
    the ``build`` mode as its own unit, which outlives the SSH connection.
``build``
    root, in that unit: wait for carrier loss, start the build unit with the secrets on its standard
    input, delete the secrets file, wait until the unit ends, empty its control group, and replace
    itself with the launcher, which writes the output disk and powers the guest off. A failure before
    that powers the guest off without writing, so the host refuses the disk.
``phase``
    the workload account, as a phase unit's ``ExecStart``: run that phase's steps in order.

Outside a lane guest every mode changes nothing and exits 2.
"""
from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import BinaryIO, Callable, Mapping, Sequence

if __package__:
    from . import output_disk
else:  # run as a script in the guest, beside output_disk.py
    import output_disk

SPEC_SCHEMA = "projectbuddy/lane-guest@1"
SPEC_FIELDS = ("schema", "account", "root", "fetch", "build", "secrets", "outputs", "maxOutputBytes", "device")
PHASE_FIELDS = ("steps", "timeoutSeconds")
GUEST_PHASES = ("fetch", "build")
DIRECTORY = "/run/projectbuddy-lane"
SPEC_NAME = "lane-guest.json"
SECRETS_NAME = "secrets"
SUPERVISOR_NAME = "supervisor.py"
LAUNCHER_NAME = "output_disk.py"
PYTHON = "/usr/bin/python3"
SLICE = "system.slice"
UNITS = {"fetch": "pb-lane-fetch.service", "build": "pb-lane-build.service"}
SUPERVISOR_UNIT = "pb-lane-supervisor.service"
# systemd's TimeoutStopSec= for every lane unit: how long a stopping unit has before SIGKILL.
STOP_TIMEOUT = 30
# A phase unit that has not ended this long after its timeout and stop timeout is given up on.
BACKSTOP_MARGIN = 60
EMPTY_TIMEOUT = 30
CARRIER_TIMEOUT = 300
POLL_SECONDS = 1
COMMAND_TIMEOUT = 120
NOT_FOUND, NOT_EXECUTABLE = 127, 126
ACCOUNT = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
SECRET_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
DEVICE = re.compile(r"/dev/disk/by-id/ata-VBOX_HARDDISK_VB[0-9a-f]{8}-[0-9a-f]{8}")
# The states a phase unit passes through between its start and its end.
UNIT_RUNNING = ("active", "deactivating")


class Refused(Exception):
    """A check failed. The message names what failed and never holds a secret value."""


@dataclass(frozen=True)
class Phase:
    steps: tuple[tuple[str, ...], ...]
    timeout: int


@dataclass(frozen=True)
class Spec:
    account: str
    root: str
    fetch: Phase
    build: Phase
    secrets: tuple[str, ...]
    outputs: tuple[str, ...]
    max_output_bytes: int
    device: str


def _stdin() -> BinaryIO:
    return os.fdopen(0, "rb")


@dataclass
class System:
    """What the supervisor reads and changes in the guest; tests replace each part."""
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    execv: Callable[[str, Sequence[str]], None] = os.execv
    kill: Callable[[int, int], None] = os.kill
    geteuid: Callable[[], int] = os.geteuid
    users: Callable[[str], pwd.struct_passwd] = pwd.getpwnam
    stdin: Callable[[], BinaryIO] = _stdin
    sysfs: str = "/sys"
    directory: str = DIRECTORY
    marker: str = output_disk.LANE_GUEST_MARKER
    owner: int = 0


# The spec

def _unique(pairs: list[tuple[str, object]]) -> dict:
    if len({key for key, _ in pairs}) != len(pairs):
        raise ValueError("a key repeats")
    return dict(pairs)


def _strings(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and "\0" not in item for item in value)


def _absolute(path: object) -> bool:
    return isinstance(path, str) and path.startswith("/") and "\0" not in path \
        and all(part not in ("", ".", "..") for part in path.split("/")[1:])


def _phase(value: object, name: str, minimum: int) -> Phase:
    if not isinstance(value, dict) or sorted(value) != sorted(PHASE_FIELDS):
        raise Refused(f"{name} must hold exactly {', '.join(PHASE_FIELDS)}")
    steps, timeout = value["steps"], value["timeoutSeconds"]
    if not isinstance(steps, list) or len(steps) < minimum or not all(_strings(step) and step for step in steps):
        raise Refused(f"{name}.steps must be at least {minimum} nonempty argument vectors")
    if type(timeout) is not int or timeout <= 0:
        raise Refused(f"{name}.timeoutSeconds must be a positive integer")
    return Phase(tuple(tuple(step) for step in steps), timeout)


def check_spec(value: object) -> Spec:
    """The spec in ``value``, or :class:`Refused` naming the first field that is not valid."""
    if not isinstance(value, dict) or sorted(value) != sorted(SPEC_FIELDS):
        raise Refused(f"the spec must hold exactly {', '.join(SPEC_FIELDS)}")
    if value["schema"] != SPEC_SCHEMA:
        raise Refused(f"the spec's schema must be {SPEC_SCHEMA}")
    if not isinstance(value["account"], str) or not ACCOUNT.fullmatch(value["account"]):
        raise Refused("account must be an account name")
    if not _absolute(value["root"]):
        raise Refused("root must be an absolute path without empty, '.', or '..' components")
    fetch, build = _phase(value["fetch"], "fetch", 0), _phase(value["build"], "build", 1)
    secrets = value["secrets"]
    if not _strings(secrets) or len(set(secrets)) != len(secrets) \
            or not all(SECRET_NAME.fullmatch(name) for name in secrets):
        raise Refused("secrets must be distinct environment variable names")
    if not _strings(value["outputs"]):
        raise Refused("outputs must be strings")
    maximum = value["maxOutputBytes"]
    if type(maximum) is not int or maximum < output_disk.SMALLEST_ARCHIVE:
        raise Refused(f"maxOutputBytes must be an integer of at least {output_disk.SMALLEST_ARCHIVE}")
    if not isinstance(value["device"], str) or not DEVICE.fullmatch(value["device"]):
        raise Refused("device must be the output disk's /dev/disk/by-id path")
    return Spec(value["account"], value["root"], fetch, build, tuple(secrets), tuple(value["outputs"]), maximum,
                value["device"])


def load_spec(system: System) -> Spec:
    path = os.path.join(system.directory, SPEC_NAME)
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle, object_pairs_hook=_unique)
    except (OSError, ValueError) as error:
        raise Refused(f"cannot read {path}: {error}") from error
    return check_spec(value)


def parse_payload(data: bytes, names: Sequence[str]) -> dict[bytes, bytes]:
    """The secrets in ``data``: exactly one ``NAME=VALUE`` and a NUL for each of ``names``, in order."""
    entries = data.split(b"\0")
    if entries.pop() != b"" or len(entries) != len(names):
        raise Refused("the secrets do not match the declared names")
    secrets: dict[bytes, bytes] = {}
    for name, entry in zip(names, entries):
        key, separator, secret = entry.partition(b"=")
        if not separator or key != name.encode("ascii"):
            raise Refused("the secrets do not match the declared names")
        secrets[key] = secret
    return secrets


# The phase runner, as the workload account

def run_phase(phase: str, spec: Spec, data: bytes, environment: Mapping[bytes, bytes], system: System) -> int:
    """Run ``phase``'s steps in order, with no shell and no standard input; return the first failure.

    A step killed by a signal is reported by the runner dying of the same signal.
    """
    secrets = parse_payload(data, spec.secrets if phase == "build" else ())
    environment = {**environment, **secrets}
    for index, argv in enumerate(getattr(spec, phase).steps, start=1):
        try:
            code = system.run(list(argv), stdin=subprocess.DEVNULL, env=environment, check=False).returncode
        except FileNotFoundError:
            print(f"{phase}-{index}: {argv[0]} was not found", file=sys.stderr)
            return NOT_FOUND
        except OSError as error:
            print(f"{phase}-{index}: cannot run {argv[0]}: {error.strerror}", file=sys.stderr)
            return NOT_EXECUTABLE
        if code < 0:
            print(f"{phase}-{index}: killed by signal {-code}", file=sys.stderr)
            if -code != signal.SIGKILL:
                signal.signal(-code, signal.SIG_DFL)
            system.kill(os.getpid(), -code)
            return 128 - code
        if code:
            print(f"{phase}-{index}: exited {code}", file=sys.stderr)
            return code
    return 0


def phase_main(phase: str, system: System) -> int:
    if system.geteuid() == 0:
        raise Refused("a phase never runs as root")
    spec = load_spec(system)
    with system.stdin() as stdin:
        data = stdin.read()
    return run_phase(phase, spec, data, os.environb, system)


# The root side

def _call(system: System, argv: Sequence[str]) -> subprocess.CompletedProcess:
    try:
        return system.run(list(argv), capture_output=True, text=True, check=False, timeout=COMMAND_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Refused(f"{argv[0]} {argv[1]} failed: {error}") from error


def workload_uid(system: System, spec: Spec) -> int:
    try:
        uid = system.users(spec.account).pw_uid
    except KeyError as error:
        raise Refused(f"the workload account {spec.account} does not exist") from error
    if uid == 0:
        raise Refused(f"the workload account {spec.account} is root")
    return uid


def require_cgroup2(system: System) -> None:
    if not os.path.isfile(os.path.join(system.sysfs, "fs", "cgroup", "cgroup.controllers")):
        raise Refused("the guest does not have the unified cgroup v2 hierarchy")


def phase_unit_argv(spec: Spec, phase: str, directory: str, uid: int | str) -> list[str]:
    """``systemd-run`` for one phase's unit, as the workload account, kept loaded after it ends."""
    stdin = f"file:{directory}/{SECRETS_NAME}" if phase == "build" else "null"
    return ["systemd-run", f"--unit={UNITS[phase]}", f"--slice={SLICE}", "--service-type=exec", f"--uid={uid}",
            f"--working-directory={spec.root}", "--expand-environment=no", "--quiet",
            "--property=RemainAfterExit=yes", f"--property=RuntimeMaxSec={getattr(spec, phase).timeout}",
            f"--property=TimeoutStopSec={STOP_TIMEOUT}", "--property=NoNewPrivileges=yes", "--property=LimitCORE=0",
            f"--property=StandardInput={stdin}", "--", PYTHON, f"{directory}/{SUPERVISOR_NAME}", "phase", phase]


def supervisor_unit_argv(directory: str) -> list[str]:
    """``systemd-run`` for the ``build`` mode, as root, in a unit that outlives the SSH connection."""
    return ["systemd-run", f"--unit={SUPERVISOR_UNIT}", f"--slice={SLICE}", "--service-type=exec",
            "--expand-environment=no", "--quiet", f"--property=TimeoutStopSec={STOP_TIMEOUT}", "--",
            PYTHON, f"{directory}/{SUPERVISOR_NAME}", "build"]


def kill_argv(unit: str) -> list[str]:
    return ["systemctl", "kill", "--kill-whom=all", "--signal=SIGKILL", "--", unit]


def launcher_argv(spec: Spec, directory: str, uid: int | str) -> list[str]:
    """The #202 launcher for the build unit, with every value attached to its option."""
    return [PYTHON, f"{directory}/{LAUNCHER_NAME}", f"--device={spec.device}", f"--root={spec.root}",
            f"--uid={uid}", f"--unit={UNITS['build']}", f"--max-output-bytes={spec.max_output_bytes}",
            *(f"--output={output}" for output in spec.outputs)]


def start(system: System, argv: Sequence[str], unit: str) -> None:
    completed = _call(system, argv)
    if completed.returncode != 0:
        raise Refused(f"systemd-run could not start {unit}: {completed.stderr.strip()}")


def wait_ended(system: System, unit: str, timeout: int) -> None:
    """Return once ``unit`` is ``failed`` or ``active``/``exited``; refuse if it is gone or never ends."""
    limit = timeout + STOP_TIMEOUT + BACKSTOP_MARGIN
    deadline = system.clock() + limit
    while True:
        found = output_disk.unit_properties(unit, system.run, ("ActiveState", "SubState"))
        state = (found.get("ActiveState", ""), found.get("SubState", ""))
        if state[0] == "failed" or state == ("active", "exited"):
            return
        if state[0] not in UNIT_RUNNING:
            raise Refused(f"{unit} is {state[0] or 'unreadable'}, so its result is lost")
        if system.clock() >= deadline:
            raise Refused(f"{unit} did not end within {limit} seconds")
        system.sleep(POLL_SECONDS)


def group_empty(system: System, unit: str) -> bool:
    """Whether ``unit``'s control group holds no process: gone, or ``populated 0``.

    The kernel refuses to remove a control group that holds a process, so a missing one is empty.
    """
    group = os.path.join(system.sysfs, "fs", "cgroup", SLICE, unit)
    try:
        with open(os.path.join(group, "cgroup.events"), encoding="ascii") as handle:
            return "populated 0" in handle.read().splitlines()
    except FileNotFoundError:
        return not os.path.lexists(group)
    except (OSError, ValueError):
        return False


def empty_group(system: System, unit: str) -> None:
    """Kill every process left in ``unit``'s control group until it is empty, for up to EMPTY_TIMEOUT."""
    deadline = system.clock() + EMPTY_TIMEOUT
    while not group_empty(system, unit):
        if system.clock() >= deadline:
            raise Refused(f"{unit}'s control group is not empty after {EMPTY_TIMEOUT} seconds")
        _call(system, kill_argv(unit))
        system.sleep(POLL_SECONDS)


def carrier_interface(system: System) -> str:
    """The guest's only network interface that has a device; refuse unless there is exactly one."""
    net = os.path.join(system.sysfs, "class", "net")
    try:
        names = sorted(os.listdir(net))
    except OSError as error:
        raise Refused(f"cannot list {net}: {error.strerror}") from error
    devices = [name for name in names if os.path.lexists(os.path.join(net, name, "device"))]
    if len(devices) != 1:
        raise Refused(f"the guest must have exactly one network device; it has {len(devices)}")
    return devices[0]


def wait_carrier_loss(system: System, interface: str) -> None:
    """Return once ``interface``'s carrier reads 0; anything else, or an error, means it may still be up."""
    path = os.path.join(system.sysfs, "class", "net", interface, "carrier")
    deadline = system.clock() + CARRIER_TIMEOUT
    while True:
        try:
            with open(path, encoding="ascii") as handle:
                if handle.read() == "0\n":
                    return
        except (OSError, ValueError):
            pass
        if system.clock() >= deadline:
            raise Refused(f"{interface} still had carrier after {CARRIER_TIMEOUT} seconds")
        system.sleep(POLL_SECONDS)


def check_secrets(system: System, names: Sequence[str]) -> None:
    """The secrets file must be root's own regular file, closed to others, holding exactly ``names``."""
    path = os.path.join(system.directory, SECRETS_NAME)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as error:
        raise Refused(f"cannot open {path}: {error.strerror}") from error
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_uid != system.owner or status.st_mode & 0o077 \
                or status.st_nlink != 1:
            raise Refused(f"{path} must be root's own regular file with one link and no group or other access")
        chunks = []
        while chunk := os.read(descriptor, output_disk.CHUNK):
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    parse_payload(b"".join(chunks), names)


def remove_secrets(system: System) -> None:
    try:
        os.unlink(os.path.join(system.directory, SECRETS_NAME))
    except FileNotFoundError:
        pass


def fetch(system: System, spec: Spec) -> str | None:
    """Run the fetch unit to its end and empty its control group; return its result."""
    uid = workload_uid(system, spec)
    if not spec.fetch.steps:
        raise Refused("the spec has no fetch step")
    require_cgroup2(system)
    unit = UNITS["fetch"]
    start(system, phase_unit_argv(spec, "fetch", system.directory, uid), unit)
    try:
        wait_ended(system, unit, spec.fetch.timeout)
    finally:
        empty_group(system, unit)
    return output_disk.result_from_unit(output_disk.unit_properties(unit, system.run))


def start_build(system: System, spec: Spec) -> None:
    """Check what does not need carrier loss while the host can still hear a refusal, then detach."""
    workload_uid(system, spec)
    require_cgroup2(system)
    carrier_interface(system)
    check_secrets(system, spec.secrets)
    start(system, supervisor_unit_argv(system.directory), SUPERVISOR_UNIT)


def build(system: System) -> list[str]:
    """Everything before the hand-off: return the launcher's argv once the build unit's group is empty.

    The secrets file is deleted on every path, as soon as the build unit has opened it or the build fails.
    """
    unit = UNITS["build"]
    try:
        spec = load_spec(system)
        uid = workload_uid(system, spec)
        require_cgroup2(system)
        interface = carrier_interface(system)
        # execv discards Python's buffers, and stdout here is the journal, not a terminal.
        print(f"build: waiting for carrier loss on {interface}", flush=True)
        wait_carrier_loss(system, interface)
        check_secrets(system, spec.secrets)
        start(system, phase_unit_argv(spec, "build", system.directory, uid), unit)
    finally:
        remove_secrets(system)
    try:
        wait_ended(system, unit, spec.build.timeout)
    finally:
        empty_group(system, unit)
    return launcher_argv(spec, system.directory, uid)


def poweroff(system: System) -> None:
    system.run(list(output_disk.POWEROFF), check=False, timeout=60)


def build_main(system: System) -> int:
    """Hand off to the launcher, which always powers off; power off here on any failure before that."""
    launcher = None
    try:
        launcher = build(system)
    except Refused as error:
        print(f"build: {error}; powering off without writing", file=sys.stderr)
        return 1
    finally:
        if launcher is None:
            poweroff(system)
    try:
        system.execv(launcher[0], launcher)
    except BaseException:
        poweroff(system)
        raise
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a lane's fetch and build phases in the guest.")
    modes = parser.add_subparsers(dest="mode", required=True)
    modes.add_parser("fetch", help="run the fetch unit and print its result")
    modes.add_parser("start-build", help="check, then start the build mode in its own unit")
    modes.add_parser("build", help="wait for carrier loss, run the build unit, then hand off to the launcher")
    phase = modes.add_parser("phase", help="run one phase's steps, as the workload account")
    phase.add_argument("phase", choices=GUEST_PHASES)
    return parser


def main(argv: Sequence[str], system: System | None = None) -> int:
    system = system or System()
    arguments = build_parser().parse_args(argv)
    if not os.path.isfile(system.marker):
        print(f"not a lane guest: {system.marker} is missing; nothing was changed", file=sys.stderr)
        return 2
    if arguments.mode == "build":
        return build_main(system)
    try:
        if arguments.mode == "phase":
            return phase_main(arguments.phase, system)
        if arguments.mode == "start-build":
            start_build(system, load_spec(system))
            print(f"start-build: {SUPERVISOR_UNIT} started")
            return 0
        result = fetch(system, load_spec(system))
    except Refused as error:
        print(f"{arguments.mode}: {error}", file=sys.stderr)
        return 1
    print(f"fetch: {result or 'no result'}")
    return 0 if result == "exited 0" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
