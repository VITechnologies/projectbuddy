"""Ubuntu lane guest supervisor (REQ-106): the phase runner, the fetch and build modes, and the hand-off."""
from __future__ import annotations

import ast
import io
import json
import os
import runpy
import signal
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from lane import declaration, output_disk, plan, supervisor  # noqa: E402
from lane.supervisor import Refused, System  # noqa: E402
from lane_box.common import LaneBoxError  # noqa: E402

DEVICE = "/dev/disk/by-id/ata-VBOX_HARDDISK_VBaabbccdd-99887766"
# Not a pwd.struct_passwd: one held until exit fails in its deallocator once the pwd module is cleared.
WORKLOAD = SimpleNamespace(pw_name="workload", pw_uid=1001)
FETCH_UNIT, BUILD_UNIT = supervisor.UNITS["fetch"], supervisor.UNITS["build"]


def spec_value(**changes) -> dict:
    value = {
        "schema": supervisor.SPEC_SCHEMA, "account": "workload", "root": "/home/workload/candidate/app",
        "fetch": {"steps": [["npm", "ci", "--ignore-scripts"]], "timeoutSeconds": 600},
        "build": {"steps": [["npm", "run", "package"], ["npm", "test"]], "timeoutSeconds": 900},
        "secrets": ["TOKEN", "OTHER"], "outputs": ["dist/a.vsix", "--odd"], "maxOutputBytes": 4096,
        "device": DEVICE,
    }
    value.update(changes)
    return value


def users(name: str) -> SimpleNamespace:
    if name == "workload":
        return WORKLOAD
    if name == "root":
        return SimpleNamespace(pw_name="root", pw_uid=0)
    raise KeyError(name)


class Guest:
    """A fake lane guest: a sysfs tree, the lane directory, scripted units, and a clock that sleep advances."""

    def __init__(self, tmp_path: Path, spec: dict | None = None, payload: bytes = b"TOKEN=s3cr3t\0OTHER=a=b\0"):
        self.sysfs = tmp_path / "sys"
        self.directory = tmp_path / "lane"
        self.directory.mkdir()
        self.marker = tmp_path / "base-bootstrap-state.json"
        self.marker.write_text("{}", encoding="utf-8")
        (self.sysfs / "fs" / "cgroup" / supervisor.SLICE).mkdir(parents=True)
        (self.sysfs / "fs" / "cgroup" / "cgroup.controllers").write_text("cpu memory pids\n", encoding="ascii")
        (self.sysfs / "class" / "net" / "lo").mkdir(parents=True)
        self.nic = self.sysfs / "class" / "net" / "enp0s3"
        (self.nic / "device").mkdir(parents=True)
        (self.nic / "carrier").write_text("1\n", encoding="ascii")
        (self.directory / supervisor.SPEC_NAME).write_text(json.dumps(spec or spec_value()), encoding="utf-8")
        self.secrets = self.directory / supervisor.SECRETS_NAME
        self.secrets.write_bytes(payload)
        self.secrets.chmod(0o600)
        self.now = 0.0
        self.calls: list[list[str]] = []
        self.states: dict[str, list[tuple[str, str]]] = {
            FETCH_UNIT: [("active", "running"), ("active", "exited")],
            BUILD_UNIT: [("active", "running"), ("active", "exited")]}
        self.results = {FETCH_UNIT: "Result=success\nExecMainCode=1\nExecMainStatus=0\n",
                        BUILD_UNIT: "Result=success\nExecMainCode=1\nExecMainStatus=0\n"}
        self.start_codes: dict[str, int] = {}
        self.kills_to_empty = 1
        self.carrier_drops_after: int | None = 1
        self.sleeps = 0
        self.executed: list[tuple[str, list[str]]] = []

    def group(self, unit: str) -> Path:
        return self.sysfs / "fs" / "cgroup" / supervisor.SLICE / unit

    def run(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[0] == "systemd-run":
            unit = argv[1].split("=", 1)[1]
            code = self.start_codes.get(unit, 0)
            if code == 0:
                self.group(unit).mkdir()
                (self.group(unit) / "cgroup.events").write_text("populated 1\nfrozen 0\n", encoding="ascii")
            return subprocess.CompletedProcess(argv, code, "", "Job failed.\n" if code else "")
        if argv[:2] == ["systemctl", "show"]:
            unit = argv[-1]
            if argv[2:4] == ["--property=ActiveState", "--property=SubState"]:
                states = self.states[unit]
                active, sub = states.pop(0) if len(states) > 1 else states[0]
                return subprocess.CompletedProcess(argv, 0, f"ActiveState={active}\nSubState={sub}\n", "")
            return subprocess.CompletedProcess(argv, 0, self.results[unit], "")
        if argv[:2] == ["systemctl", "kill"]:
            self.kills_to_empty -= 1
            if self.kills_to_empty <= 0:
                (self.group(argv[-1]) / "cgroup.events").write_text("populated 0\nfrozen 0\n", encoding="ascii")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv == list(output_disk.POWEROFF):
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(argv)

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.sleeps += 1
        if self.carrier_drops_after is not None and self.sleeps >= self.carrier_drops_after:
            (self.nic / "carrier").write_text("0\n", encoding="ascii")

    def execv(self, path: str, argv) -> None:
        self.executed.append((path, list(argv)))

    def system(self, **changes) -> System:
        values = dict(run=self.run, sleep=self.sleep, clock=lambda: self.now, execv=self.execv, users=users,
                      sysfs=str(self.sysfs), directory=str(self.directory), marker=str(self.marker),
                      owner=os.getuid())
        values.update(changes)
        return System(**values)

    def commands(self) -> list[list[str]]:
        return [call[:2] for call in self.calls]


def loaded(**changes) -> supervisor.Spec:
    return supervisor.check_spec(spec_value(**changes))


# The spec

def test_a_valid_spec_loads(tmp_path):
    spec = supervisor.load_spec(Guest(tmp_path, spec_value(fetch={"steps": [], "timeoutSeconds": 1})).system())
    assert spec.fetch == supervisor.Phase((), 1) and spec.build.timeout == 900
    assert spec.secrets == ("TOKEN", "OTHER") and spec.outputs == ("dist/a.vsix", "--odd") and spec.device == DEVICE


@pytest.mark.parametrize("changes", [
    {"extra": 1}, {"schema": "projectbuddy/lane-guest@2"}, {"account": "Work"}, {"account": 5}, {"account": "a" * 33},
    {"root": "relative/path"}, {"root": "/"}, {"root": "/a//b"}, {"root": "/a/../b"}, {"root": "/a/./b"},
    {"root": "/a\0b"}, {"root": 1},
    {"fetch": []}, {"fetch": {"steps": []}}, {"fetch": {"steps": "npm ci", "timeoutSeconds": 1}},
    {"build": {"steps": [], "timeoutSeconds": 1}}, {"build": {"steps": [[]], "timeoutSeconds": 1}},
    {"build": {"steps": [["a", 1]], "timeoutSeconds": 1}}, {"build": {"steps": [["a\0"]], "timeoutSeconds": 1}},
    {"build": {"steps": [["a"]], "timeoutSeconds": 0}}, {"build": {"steps": [["a"]], "timeoutSeconds": True}},
    {"build": {"steps": [["a"]], "timeoutSeconds": 1.5}},
    {"secrets": ["A", "A"]}, {"secrets": ["1A"]}, {"secrets": ["A-B"]}, {"secrets": "A"},
    {"outputs": [1]}, {"outputs": "a"},
    {"maxOutputBytes": output_disk.SMALLEST_ARCHIVE - 1}, {"maxOutputBytes": True}, {"maxOutputBytes": 4096.0},
    {"device": "/dev/sdb"}, {"device": DEVICE.upper()}, {"device": DEVICE + "\n"}, {"device": None},
])
def test_an_invalid_spec_is_refused(changes):
    with pytest.raises(Refused):
        supervisor.check_spec(spec_value(**changes))


def test_a_spec_missing_a_field_or_not_an_object_is_refused():
    value = spec_value()
    del value["device"]
    for bad in (value, [], "spec"):
        with pytest.raises(Refused, match="exactly"):
            supervisor.check_spec(bad)


@pytest.mark.parametrize("text", ['{"schema": 1, "schema": 2}', "{", "\xff"])
def test_an_unreadable_spec_is_refused(tmp_path, text):
    guest = Guest(tmp_path)
    (guest.directory / supervisor.SPEC_NAME).write_bytes(text.encode("latin-1"))
    with pytest.raises(Refused, match="cannot read"):
        supervisor.load_spec(guest.system())


def test_a_missing_spec_is_refused(tmp_path):
    guest = Guest(tmp_path)
    (guest.directory / supervisor.SPEC_NAME).unlink()
    with pytest.raises(Refused, match="cannot read"):
        supervisor.load_spec(guest.system())


def test_the_guest_rules_match_the_engine():
    assert supervisor.SECRET_NAME.pattern == declaration.SECRET_NAME.pattern
    assert supervisor.GUEST_PHASES == plan.GUEST_PHASES
    assert supervisor.LAUNCHER_NAME == Path(output_disk.__file__).name
    assert supervisor.SUPERVISOR_NAME == Path(supervisor.__file__).name
    assert all(output_disk.UNIT_NAME.fullmatch(unit) for unit in (*supervisor.UNITS.values(),
                                                                   supervisor.SUPERVISOR_UNIT))


# The output device

@pytest.mark.parametrize("uuid,path", [
    ("aabbccdd-eeff-0011-2233-445566778899", DEVICE),
    ("AABBCCDD-EEFF-0011-2233-445566778899", DEVICE),
    ("00000001-0000-0000-0000-000000000002", "/dev/disk/by-id/ata-VBOX_HARDDISK_VB00000001-02000000"),
])
def test_the_output_device_follows_the_ahci_serial(uuid, path):
    assert plan.output_device(uuid) == path and supervisor.DEVICE.fullmatch(path)


@pytest.mark.parametrize("uuid", ["", "aabbccdd-eeff-0011-2233-44556677889", "{aabbccdd-eeff-0011-2233-445566778899}"])
def test_an_output_device_needs_a_medium_uuid(uuid):
    with pytest.raises(LaneBoxError):
        plan.output_device(uuid)


# The secrets payload

def test_a_payload_gives_each_declared_name_its_value():
    assert supervisor.parse_payload(b"A=1\0B=x=y\0C=\0", ["A", "B", "C"]) == {b"A": b"1", b"B": b"x=y", b"C": b""}
    assert supervisor.parse_payload(b"", []) == {}


@pytest.mark.parametrize("data,names", [
    (b"A=1", ["A"]), (b"B=1\0A=2\0", ["A", "B"]), (b"A=1\0B=2\0", ["A"]), (b"A\0", ["A"]), (b"", ["A"]),
    (b"A=1\0", []), (b"\0", []), (b"AB=1\0", ["A"]),
])
def test_a_payload_that_does_not_match_the_names_is_refused(data, names):
    with pytest.raises(Refused) as refused:
        supervisor.parse_payload(data, names)
    assert b"1" not in str(refused.value).encode() and "2" not in str(refused.value)


# The phase runner

class Steps:
    def __init__(self, *codes):
        self.codes = list(codes)
        self.calls = []

    def run(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        code = self.codes.pop(0)
        if isinstance(code, BaseException):
            raise code
        return subprocess.CompletedProcess(argv, code)


def test_the_build_steps_run_in_order_with_the_secrets_and_no_stdin():
    steps = Steps(0, 0)
    code = supervisor.run_phase("build", loaded(), b"TOKEN=t\0OTHER=o\0", {b"PATH": b"/usr/bin"}, System(run=steps.run))
    assert code == 0 and [argv for argv, _ in steps.calls] == [["npm", "run", "package"], ["npm", "test"]]
    for _, kwargs in steps.calls:
        assert kwargs == {"stdin": subprocess.DEVNULL, "env": {b"PATH": b"/usr/bin", b"TOKEN": b"t", b"OTHER": b"o"},
                          "check": False}


def test_the_fetch_steps_get_no_secret():
    steps = Steps(0)
    assert supervisor.run_phase("fetch", loaded(), b"", {b"HOME": b"/h"}, System(run=steps.run)) == 0
    assert steps.calls == [(["npm", "ci", "--ignore-scripts"],
                            {"stdin": subprocess.DEVNULL, "env": {b"HOME": b"/h"}, "check": False})]
    with pytest.raises(Refused):
        supervisor.run_phase("fetch", loaded(), b"TOKEN=t\0", {}, System(run=steps.run))


def test_a_failing_step_stops_the_phase(capsys):
    steps = Steps(3)
    assert supervisor.run_phase("build", loaded(), b"TOKEN=\0OTHER=\0", {}, System(run=steps.run)) == 3
    assert len(steps.calls) == 1 and "build-1: exited 3" in capsys.readouterr().err


@pytest.mark.parametrize("error,code", [(FileNotFoundError(2, "No such file"), supervisor.NOT_FOUND),
                                        (PermissionError(13, "Permission denied"), supervisor.NOT_EXECUTABLE)])
def test_a_step_that_cannot_run_stops_the_phase(error, code):
    steps = Steps(0, error)
    assert supervisor.run_phase("build", loaded(), b"TOKEN=\0OTHER=\0", {}, System(run=steps.run)) == code
    assert len(steps.calls) == 2


@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGKILL])
def test_a_step_killed_by_a_signal_is_raised_again(number):
    killed = []
    steps = Steps(-number)
    system = System(run=steps.run, kill=lambda pid, sent: killed.append((pid, sent)))
    assert supervisor.run_phase("build", loaded(), b"TOKEN=\0OTHER=\0", {}, system) == 128 + number
    assert killed == [(os.getpid(), number)] and signal.getsignal(signal.SIGTERM) == signal.SIG_DFL


def run_child(code: str) -> subprocess.CompletedProcess:
    prelude = f"import sys; sys.path.insert(0, {str(SCRIPTS)!r})\nfrom lane import supervisor\n"
    return subprocess.run([sys.executable, "-c", prelude + textwrap.dedent(code)], capture_output=True, text=True,
                          check=False, timeout=60)


def test_a_real_phase_runs_its_steps_and_dies_of_a_steps_signal(tmp_path):
    seen = tmp_path / "seen"
    spec = spec_value(build={"steps": [
        [sys.executable, "-c", f"import os; open({str(seen)!r}, 'w').write(os.environ['TOKEN'] + '|' + "
                               "str(os.read(0, 1)))"],
        [sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"],
        [sys.executable, "-c", f"open({str(tmp_path / 'never')!r}, 'w')"],
    ], "timeoutSeconds": 5})
    completed = run_child(f"""
        spec = supervisor.check_spec({spec!r})
        sys.exit(supervisor.run_phase("build", spec, b"TOKEN=v\\0OTHER=w\\0", {{}}, supervisor.System()))
    """)
    assert completed.returncode == -signal.SIGTERM, completed.stderr
    assert seen.read_text() == "v|b''" and not (tmp_path / "never").exists()
    assert "build-2: killed by signal 15" in completed.stderr


def test_the_phase_mode_reads_and_closes_its_stdin(tmp_path):
    guest = Guest(tmp_path)
    stdin = io.BytesIO(b"TOKEN=t\0OTHER=o\0")
    steps = Steps(0, 0)
    assert supervisor.main(["phase", "build"], guest.system(run=steps.run, stdin=lambda: stdin)) == 0
    assert stdin.closed and steps.calls[0][1]["env"][b"TOKEN"] == b"t"


def test_the_phase_payload_comes_from_descriptor_zero_as_bytes():
    saved = os.dup(0)
    read_end, write_end = os.pipe()
    os.write(write_end, b"TOKEN=t\0")
    os.close(write_end)
    try:
        os.dup2(read_end, 0)
        os.close(read_end)
        with System().stdin() as stream:
            assert stream.read() == b"TOKEN=t\0"
    finally:
        os.dup2(saved, 0)
        os.close(saved)


def test_the_phase_mode_never_runs_as_root(tmp_path, capsys):
    guest = Guest(tmp_path)
    steps = Steps()
    assert supervisor.main(["phase", "fetch"], guest.system(run=steps.run, geteuid=lambda: 0)) == 1
    assert steps.calls == [] and "never runs as root" in capsys.readouterr().err


# The root modes

@pytest.mark.parametrize("argv", [["fetch"], ["start-build"], ["build"], ["phase", "build"]])
def test_outside_a_lane_guest_every_mode_changes_nothing(tmp_path, argv, capsys):
    guest = Guest(tmp_path)
    assert supervisor.main(argv, guest.system(marker=str(tmp_path / "absent"))) == 2
    assert guest.calls == [] and guest.secrets.exists() and "not a lane guest" in capsys.readouterr().err


def test_the_fetch_unit_runs_to_its_end_and_its_group_is_emptied(tmp_path, capsys):
    guest = Guest(tmp_path)
    assert supervisor.main(["fetch"], guest.system()) == 0
    assert guest.calls[0] == supervisor.phase_unit_argv(loaded(), "fetch", str(guest.directory), WORKLOAD.pw_uid)
    assert guest.commands() == [["systemd-run", f"--unit={FETCH_UNIT}"], *[["systemctl", "show"]] * 2,
                                ["systemctl", "kill"], ["systemctl", "show"]]
    assert guest.calls[3] == supervisor.kill_argv(FETCH_UNIT)
    assert "--property=StandardInput=null" in guest.calls[0] and guest.secrets.exists()
    assert capsys.readouterr().out == "fetch: exited 0\n"


@pytest.mark.parametrize("result,printed", [
    ("Result=exit-code\nExecMainCode=1\nExecMainStatus=3\n", "fetch: exited 3"),
    ("Result=timeout\nExecMainCode=2\nExecMainStatus=15\n", "fetch: timeout"),
    ("Result=signal\nExecMainCode=2\nExecMainStatus=9\n", "fetch: killed 9"),
    # systemd counts a death by SIGTERM as clean, so the unit is active/exited with Result=success.
    ("Result=success\nExecMainCode=2\nExecMainStatus=15\n", "fetch: killed 15"),
    ("Result=success\nExecMainCode=0\nExecMainStatus=0\n", "fetch: no result"),
])
def test_a_fetch_that_did_not_exit_0_fails(tmp_path, capsys, result, printed):
    guest = Guest(tmp_path)
    guest.states[FETCH_UNIT] = [("deactivating", "stop-sigterm"), ("failed", "failed")]
    guest.results[FETCH_UNIT] = result
    assert supervisor.main(["fetch"], guest.system()) == 1
    assert capsys.readouterr().out == printed + "\n"


@pytest.mark.parametrize("arrange,message", [
    (lambda guest: (guest.directory / supervisor.SPEC_NAME).write_text(
        json.dumps(spec_value(fetch={"steps": [], "timeoutSeconds": 1}))), "no fetch step"),
    (lambda guest: (guest.directory / supervisor.SPEC_NAME).write_text(json.dumps(spec_value(account="root"))),
     "is root"),
    (lambda guest: (guest.directory / supervisor.SPEC_NAME).write_text(json.dumps(spec_value(account="nobody9"))),
     "does not exist"),
    (lambda guest: (guest.sysfs / "fs" / "cgroup" / "cgroup.controllers").unlink(), "cgroup v2"),
    (lambda guest: guest.start_codes.update({FETCH_UNIT: 1}), "could not start"),
])
def test_a_fetch_that_cannot_start_is_refused_before_any_step(tmp_path, capsys, arrange, message):
    guest = Guest(tmp_path)
    arrange(guest)
    assert supervisor.main(["fetch"], guest.system()) == 1
    assert [call[0] for call in guest.calls] in ([], ["systemd-run"]) and message in capsys.readouterr().err


def test_a_command_that_times_out_is_refused(tmp_path, capsys):
    guest = Guest(tmp_path)

    def run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    assert supervisor.main(["fetch"], guest.system(run=run)) == 1
    assert "systemd-run --unit=pb-lane-fetch.service failed" in capsys.readouterr().err


def test_a_unit_that_disappears_loses_its_result_and_is_still_emptied(tmp_path, capsys):
    guest = Guest(tmp_path)
    guest.states[FETCH_UNIT] = [("inactive", "dead")]
    assert supervisor.main(["fetch"], guest.system()) == 1
    assert guest.calls[-1] == supervisor.kill_argv(FETCH_UNIT) and "inactive, so its result is lost" in \
        capsys.readouterr().err


def test_an_unreadable_unit_is_refused(tmp_path, capsys):
    guest = Guest(tmp_path)
    run = guest.run

    def failing_show(argv, **kwargs):
        if argv[:2] == ["systemctl", "show"]:
            return subprocess.CompletedProcess(argv, 1, "", "")
        return run(argv, **kwargs)

    assert supervisor.main(["fetch"], guest.system(run=failing_show)) == 1
    assert "unreadable" in capsys.readouterr().err


def test_a_unit_that_never_ends_is_given_up_after_its_backstop(tmp_path, capsys):
    guest = Guest(tmp_path)
    guest.states[FETCH_UNIT] = [("active", "running")]
    assert supervisor.main(["fetch"], guest.system()) == 1
    limit = 600 + supervisor.STOP_TIMEOUT + supervisor.BACKSTOP_MARGIN
    assert f"did not end within {limit} seconds" in capsys.readouterr().err
    assert guest.calls[-1] == supervisor.kill_argv(FETCH_UNIT) and limit <= guest.now <= limit + 2


def test_a_group_that_never_empties_is_refused(tmp_path, capsys):
    guest = Guest(tmp_path)
    guest.kills_to_empty = 10 ** 6
    assert supervisor.main(["fetch"], guest.system()) == 1
    assert "control group is not empty after 30 seconds" in capsys.readouterr().err
    assert [call[:2] for call in guest.calls].count(["systemctl", "kill"]) == supervisor.EMPTY_TIMEOUT


def test_a_group_is_empty_only_when_gone_or_unpopulated(tmp_path):
    guest = Guest(tmp_path)
    system = guest.system()
    assert supervisor.group_empty(system, FETCH_UNIT)
    guest.group(FETCH_UNIT).mkdir()
    assert not supervisor.group_empty(system, FETCH_UNIT)
    (guest.group(FETCH_UNIT) / "cgroup.events").mkdir()
    assert not supervisor.group_empty(system, FETCH_UNIT)
    (guest.group(FETCH_UNIT) / "cgroup.events").rmdir()
    (guest.group(FETCH_UNIT) / "cgroup.events").write_bytes(b"populated 0\xff\n")
    assert not supervisor.group_empty(system, FETCH_UNIT)
    (guest.group(FETCH_UNIT) / "cgroup.events").write_text("populated 1\nfrozen 0\n")
    assert not supervisor.group_empty(system, FETCH_UNIT)
    (guest.group(FETCH_UNIT) / "cgroup.events").write_text("populated 0\nfrozen 0\n")
    assert supervisor.group_empty(system, FETCH_UNIT)


def test_start_build_checks_then_detaches_the_build_mode(tmp_path, capsys):
    guest = Guest(tmp_path)
    assert supervisor.main(["start-build"], guest.system()) == 0
    assert guest.calls == [supervisor.supervisor_unit_argv(str(guest.directory))]
    assert guest.secrets.exists() and capsys.readouterr().out == f"start-build: {supervisor.SUPERVISOR_UNIT} started\n"


def second_nic(guest):
    (guest.sysfs / "class" / "net" / "enp0s8" / "device").mkdir(parents=True)


def no_net(guest):
    for path in sorted((guest.sysfs / "class").rglob("*"), reverse=True):
        path.rmdir() if path.is_dir() else path.unlink()
    (guest.sysfs / "class").rmdir()


def symlinked_secrets(guest):
    target = guest.directory / "target"
    guest.secrets.rename(target)
    guest.secrets.symlink_to(target)


SECRETS_REFUSALS = [
    (second_nic, "exactly one network device; it has 2"),
    (no_net, "cannot list"),
    (lambda guest: guest.secrets.unlink(), "cannot open"),
    (symlinked_secrets, "cannot open"),
    (lambda guest: guest.secrets.chmod(0o640), "no group or other access"),
    (lambda guest: os.link(guest.secrets, guest.directory / "second"), "one link"),
    (lambda guest: guest.secrets.write_bytes(b"TOKEN=x\0"), "do not match"),
]


@pytest.mark.parametrize("arrange,message", SECRETS_REFUSALS)
def test_start_build_refuses_while_the_host_can_still_hear_it(tmp_path, capsys, arrange, message):
    guest = Guest(tmp_path)
    arrange(guest)
    assert supervisor.main(["start-build"], guest.system()) == 1
    assert guest.calls == [] and message in capsys.readouterr().err


def test_secrets_root_does_not_own_are_refused(tmp_path, capsys):
    guest = Guest(tmp_path)
    assert supervisor.main(["start-build"], guest.system(owner=os.getuid() + 1)) == 1
    assert "root's own regular file" in capsys.readouterr().err


def test_the_build_writes_its_progress_before_the_hand_off(tmp_path, monkeypatch):
    written = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(written, encoding="utf-8"))
    at_exec = []
    guest = Guest(tmp_path)
    assert supervisor.main(["build"], guest.system(execv=lambda path, argv: at_exec.append(written.getvalue()))) == 0
    assert at_exec == [b"build: waiting for carrier loss on enp0s3\n"]


def test_the_build_waits_for_carrier_loss_then_hands_off_to_the_launcher(tmp_path, capsys):
    guest = Guest(tmp_path)
    assert supervisor.main(["build"], guest.system()) == 0
    spec = loaded()
    assert guest.calls[0] == supervisor.phase_unit_argv(spec, "build", str(guest.directory), WORKLOAD.pw_uid)
    assert f"--property=StandardInput=file:{guest.secrets}" in guest.calls[0]
    assert guest.commands() == [["systemd-run", f"--unit={BUILD_UNIT}"], *[["systemctl", "show"]] * 2,
                                ["systemctl", "kill"]]
    launcher = supervisor.launcher_argv(spec, str(guest.directory), WORKLOAD.pw_uid)
    assert guest.executed == [(supervisor.PYTHON, launcher)] and not guest.secrets.exists()
    assert guest.sleeps >= 1 and list(output_disk.POWEROFF) not in guest.calls
    assert "s3cr3t" not in repr(guest.calls) + repr(guest.executed) + str(capsys.readouterr())
    arguments = output_disk.parse_arguments(launcher[2:])
    assert (arguments.device, arguments.root, arguments.uid, arguments.unit, arguments.max_output_bytes,
            arguments.output) == (DEVICE, spec.root, WORKLOAD.pw_uid, BUILD_UNIT, 4096, ["dist/a.vsix", "--odd"])


def test_a_carrier_that_never_drops_powers_off_before_any_step(tmp_path, capsys):
    guest = Guest(tmp_path)
    guest.carrier_drops_after = None
    assert supervisor.main(["build"], guest.system()) == 1
    assert guest.calls == [list(output_disk.POWEROFF)] and not guest.secrets.exists() and guest.executed == []
    assert guest.now == supervisor.CARRIER_TIMEOUT and "still had carrier after 300 seconds" in capsys.readouterr().err


def test_an_unreadable_carrier_is_not_carrier_loss(tmp_path):
    guest = Guest(tmp_path)
    (guest.nic / "carrier").unlink()
    guest.carrier_drops_after = 3
    assert supervisor.main(["build"], guest.system()) == 0 and guest.sleeps >= 3


@pytest.mark.parametrize("arrange,message", [
    *SECRETS_REFUSALS[2:],
    (lambda guest: guest.start_codes.update({BUILD_UNIT: 1}), "could not start"),
    (lambda guest: (guest.directory / supervisor.SPEC_NAME).write_text("[]"), "exactly"),
    (lambda guest: (guest.directory / supervisor.SPEC_NAME).write_text(json.dumps(spec_value(account="root"))),
     "is root"),
])
def test_a_build_that_cannot_start_powers_off_without_writing(tmp_path, capsys, arrange, message):
    guest = Guest(tmp_path)
    arrange(guest)
    assert supervisor.main(["build"], guest.system()) == 1
    assert guest.calls[-1] == list(output_disk.POWEROFF) and guest.executed == []
    assert not guest.secrets.exists() and message in capsys.readouterr().err


def test_a_build_unit_that_never_ends_is_emptied_and_powers_off_without_writing(tmp_path):
    guest = Guest(tmp_path)
    guest.states[BUILD_UNIT] = [("active", "running")]
    assert supervisor.main(["build"], guest.system()) == 1
    assert guest.calls[-2:] == [supervisor.kill_argv(BUILD_UNIT), list(output_disk.POWEROFF)]
    assert guest.executed == []


def test_a_launcher_that_cannot_be_executed_still_powers_off(tmp_path):
    guest = Guest(tmp_path)

    def execv(path, argv):
        raise FileNotFoundError(2, "No such file", path)

    with pytest.raises(FileNotFoundError):
        supervisor.main(["build"], guest.system(execv=execv))
    assert guest.calls[-1] == list(output_disk.POWEROFF)


def test_an_unexpected_error_in_the_build_still_powers_off(tmp_path):
    guest = Guest(tmp_path)
    run = guest.run

    def broken(argv, **kwargs):
        if argv[0] == "systemd-run":
            raise RuntimeError("unexpected")
        return run(argv, **kwargs)

    with pytest.raises(RuntimeError):
        supervisor.main(["build"], guest.system(run=broken))
    assert guest.calls[-1] == list(output_disk.POWEROFF) and not guest.secrets.exists()


def test_no_unit_argv_holds_a_secret_value():
    spec = loaded()
    for argv in (supervisor.phase_unit_argv(spec, "fetch", "/d", 1), supervisor.phase_unit_argv(spec, "build", "/d", 1),
                 supervisor.supervisor_unit_argv("/d"), supervisor.launcher_argv(spec, "/d", 1)):
        assert not any("StandardInputData" in item or "s3cr3t" in item for item in argv)
    fetch_argv = supervisor.phase_unit_argv(spec, "fetch", "/d", 1)
    assert "--property=StandardInput=null" in fetch_argv and "--property=NoNewPrivileges=yes" in fetch_argv


# The script

def test_the_supervisor_runs_as_a_script(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["supervisor.py", "--help"])
    monkeypatch.syspath_prepend(str(SCRIPTS / "lane"))
    with pytest.raises(SystemExit) as stopped:
        runpy.run_path(str(SCRIPTS / "lane" / "supervisor.py"), run_name="__main__")
    assert stopped.value.code == 0 and "start-build" in capsys.readouterr().out


def test_the_script_outside_a_lane_guest_changes_nothing():
    if os.path.exists(output_disk.LANE_GUEST_MARKER):
        pytest.skip("this host is a lane guest")
    completed = subprocess.run([sys.executable, str(SCRIPTS / "lane" / "supervisor.py"), "fetch"],
                               capture_output=True, text=True, check=False, timeout=60)
    assert completed.returncode == 2 and "not a lane guest" in completed.stderr


def test_the_supervisor_needs_only_the_standard_library_and_the_launcher():
    tree = ast.parse((SCRIPTS / "lane" / "supervisor.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported |= {alias.name for alias in node.names} if node.level else {node.module.split(".")[0]}
    assert imported - set(sys.stdlib_module_names) == {"output_disk"}, imported
