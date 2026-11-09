"""Lane teardown (VITechnologies/projectbuddy#185, REQ-102, TEST-104): allow-list, lock, receipt, verifier."""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import runpy
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from lane import cli, teardown  # noqa: E402
from lane.common import MACHINE_ID  # noqa: E402
from lane_box.common import LaneBoxError  # noqa: E402

UUID = "0a0a0a0a-1111-4222-8333-444444444444"
OTHER_UUID = "0b0b0b0b-5555-4666-8777-888888888888"
NAME = "pb-lane-0123456789ab"
BOX_BYTES = b"lane box bytes"
BOX_SHA256 = hashlib.sha256(BOX_BYTES).hexdigest()
REFUSED_STATES = ("livesnapshotting", "onlinesnapshotting", "snapshotting", "saved", "stopping", "teleporting",
                  "a-state-teardown-does-not-know")

VBOXMANAGE_STUB = """
import json, os, sys, time
path = os.environ["LANE_FAKE_VM"]
modes = set(filter(None, os.environ.get("LANE_FAKE_VBOX", "").split(",")))
args = sys.argv[1:]
with open(path + ".log", "a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\\n")
state = json.load(open(path, encoding="utf-8"))
vms = state["vms"]


def save():
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(state, stream)


def become(uuid, value):
    if value == "absent":
        del vms[uuid]
    else:
        vms[uuid]["VMState"] = value
    save()


if args == ["list", "vms"]:
    if "list-fail" in modes:
        sys.exit(1)
    for vm in vms.values():
        print('"%s" {%s}' % (vm["name"], vm["UUID"]))
elif len(args) == 3 and args[0] == "showvminfo" and args[2] == "--machinereadable":
    vm = vms.get(args[1])
    if vm is not None and "vanish-on-show" in modes:
        become(args[1], "absent")
        vm = None
    if vm is None or "show-fail" in modes:
        sys.exit(1)
    shown = dict(vm)
    if "wrong-uuid" in modes:
        shown["UUID"] = "00000000-0000-4000-8000-000000000000"
    if "no-state" in modes:
        del shown["VMState"]
    for key, value in shown.items():
        print('%s="%s"' % (key, value))
    pending = state["pending"].get(args[1])
    if pending:
        become(args[1], pending.pop(0))
elif len(args) == 3 and args[0] == "controlvm" and args[2] == "poweroff":
    if "poweroff-sleep" in modes:
        time.sleep(float(os.environ.get("LANE_FAKE_SLEEP", "1")))
    if "poweroff-fail" in modes or args[1] not in vms:
        sys.exit(1)
    after = list(state["afterPoweroff"])
    state["pending"][args[1]] = after[1:]
    become(args[1], after[0])
elif len(args) == 3 and args[0] == "unregistervm" and args[2] == "--delete":
    if "delete-fail" in modes or args[1] not in vms:
        sys.exit(1)
    if "delete-noop" not in modes:
        become(args[1], "absent")
else:
    sys.exit(2)
"""


def private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


class Host:
    """A private host layout with a bound run directory, a box, and a stub VBoxManage."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = private_dir(tmp_path / "host")
        self.locks = private_dir(self.root / "locks")
        self.out = private_dir(self.root / "out")
        self.run_dir = private_dir(self.root / "run")
        machine_id = self.run_dir / MACHINE_ID
        machine_id.parent.mkdir(parents=True)
        machine_id.write_text(UUID, encoding="utf-8")
        (self.run_dir / "vagrant-home").mkdir()
        (self.run_dir / "vagrant-home" / "box.vmdk").write_bytes(b"imported copy")
        self.box = self.root / "lane.box"
        self.box.write_bytes(BOX_BYTES)
        self.receipt = self.out / "teardown.json"
        bin_dir = private_dir(self.root / "bin")
        self.vboxmanage = bin_dir / "VBoxManage"
        self.vboxmanage.write_text(f"#!{sys.executable}" + VBOXMANAGE_STUB, encoding="utf-8")
        self.vboxmanage.chmod(0o700)
        self.vm_state = self.root / "vm-state.json"
        self.set_vm()
        monkeypatch.setenv("LANE_FAKE_VM", str(self.vm_state))
        monkeypatch.delenv("LANE_FAKE_VBOX", raising=False)
        monkeypatch.setattr(teardown, "POLL_INTERVAL", 0)

    def set_vm(self, state: str = "running", name: str = NAME, after_poweroff: list[str] | None = None,
               present: bool = True) -> None:
        vms = {OTHER_UUID: {"name": "golden-appliance", "UUID": OTHER_UUID, "VMState": "poweroff"}}
        if present:
            vms[UUID] = {"name": name, "UUID": UUID, "VMState": state}
        value = {"vms": vms, "pending": {}, "afterPoweroff": after_poweroff or ["poweroff"]}
        self.vm_state.write_text(json.dumps(value), encoding="utf-8")

    def vms(self) -> dict:
        return json.loads(self.vm_state.read_text(encoding="utf-8"))["vms"]

    def calls(self) -> list[list[str]]:
        log = Path(f"{self.vm_state}.log")
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    def mutations(self) -> list[list[str]]:
        return [call for call in self.calls() if call[0] in ("controlvm", "unregistervm")]

    def args(self, run: bool = False, **overrides) -> list[str]:
        values = {
            "--uuid": UUID, "--run-dir": str(self.run_dir), "--box": str(self.box), "--box-sha256": BOX_SHA256,
            "--receipt-out": str(self.receipt), "--lock-dir": str(self.locks), "--vboxmanage": str(self.vboxmanage),
        }
        values.update(overrides)
        argv = ["teardown"]
        for key, value in values.items():
            if value is not None:
                argv += [key, value]
        return argv + (["--run"] if run else [])

    def request(self, receipt: Path | None = None) -> teardown.Request:
        return teardown.Request(uuid=UUID, run_dir=self.run_dir, box=self.box, box_sha256=BOX_SHA256,
                                lock_dir=self.locks, receipt_out=receipt or self.receipt)

    def receipt_json(self) -> dict:
        return json.loads(self.receipt.read_text(encoding="utf-8"))

    def rewrite_receipt(self, value: dict) -> None:
        self.receipt.write_text(json.dumps(value), encoding="utf-8")
        Path(f"{self.receipt}.sha256").write_text(
            f"{hashlib.sha256(self.receipt.read_bytes()).hexdigest()}  {self.receipt.name}\n", encoding="utf-8")


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Host:
    return Host(tmp_path, monkeypatch)


def run_cli(argv: list[str], capsys) -> tuple[int, str, str]:
    code = cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def verify(host: Host, capsys, *extra: str) -> tuple[int, str, str]:
    return run_cli(["verify-teardown", str(host.receipt), *extra], capsys)


def removed_receipt(host: Host, capsys) -> dict:
    assert run_cli(host.args(run=True), capsys)[0] == 0
    return host.receipt_json()


# ---------------------------------------------------------------- allow-list and plan


@pytest.mark.parametrize("state, action", [
    ("running", "poweroff"), ("paused", "poweroff"), ("poweroff", "delete"), ("aborted", "delete"),
    *[(state, None) for state in REFUSED_STATES],
])
def test_decide_is_the_req_102_allow_list(state, action):
    assert teardown.decide(state) == action


@pytest.mark.parametrize("state, steps", [
    ("running", [f"controlvm {UUID} poweroff", f"unregistervm {UUID} --delete"]),
    ("paused", [f"controlvm {UUID} poweroff", f"unregistervm {UUID} --delete"]),
    ("aborted", [f"unregistervm {UUID} --delete"]),
])
def test_dry_run_prints_the_plan_and_changes_nothing(host, capsys, state, steps):
    host.set_vm(state)
    code, out, _ = run_cli(host.args(), capsys)
    assert code == 0
    assert out.splitlines()[:5] == ["Lane teardown: PLAN", f"uuid={UUID}", f"name={NAME}", f"observed={state}",
                                    "run_directory=bound"]
    assert [line[len("step=VBoxManage "):] for line in out.splitlines() if line.startswith("step=VBoxManage")] == steps
    assert f"step=remove {host.run_dir}" in out and f"step=check the box SHA-256 is {BOX_SHA256}" in out
    assert host.mutations() == [] and host.run_dir.is_dir() and not host.receipt.exists()


def test_dry_run_of_an_absent_vm_without_a_run_directory_plans_no_command(host, capsys):
    host.set_vm(present=False)
    _remove_run_dir(host)
    code, out, _ = run_cli(host.args(), capsys)
    assert code == 0 and "observed=absent" in out and "run_directory=absent" in out
    assert "step=VBoxManage" not in out and "step=remove" not in out


@pytest.mark.parametrize("state", REFUSED_STATES)
def test_dry_run_refuses_a_state_outside_the_allow_list(host, capsys, state):
    host.set_vm(state)
    code, _, err = run_cli(host.args(), capsys)
    assert code == 2 and "outside the teardown allow-list" in err
    assert host.mutations() == []


# ---------------------------------------------------------------- teardown


@pytest.mark.parametrize("state, commands", [
    ("running", ["poweroff", "delete"]), ("paused", ["poweroff", "delete"]),
    ("poweroff", ["delete"]), ("aborted", ["delete"]),
])
def test_teardown_from_each_allowed_state(host, capsys, state, commands):
    host.set_vm(state)
    code, out, err = run_cli(host.args(run=True), capsys)
    assert (code, err) == (0, "")
    assert f"Lane teardown: REMOVED: {UUID}" in out
    expected = [teardown.command_for(action, UUID) for action in commands]
    assert host.mutations() == expected
    assert all(UUID in call for call in host.calls() if call != ["list", "vms"])
    assert UUID not in host.vms() and OTHER_UUID in host.vms()
    assert not host.run_dir.exists()
    receipt = host.receipt_json()
    assert receipt["outcome"] == "removed" and receipt["failures"] == []
    assert receipt["instance"] == {"uuid": UUID, "name": NAME}
    assert receipt["steps"][0] == {"observed": state} and receipt["steps"][-1] == {"observed": "absent"}
    assert [step["command"] for step in receipt["steps"] if "command" in step] == expected
    assert receipt["runDirectory"] == "removed"
    assert receipt["box"] == {"expectedSha256": BOX_SHA256, "sha256AfterTeardown": BOX_SHA256}
    assert oct(host.receipt.stat().st_mode & 0o777) == "0o600"
    assert verify(host, capsys, "--uuid", UUID, "--box-sha256", BOX_SHA256)[:2] == (
        0, f"Lane teardown receipt: PASS: removed: {UUID}\n")


def test_teardown_waits_until_the_poweroff_is_observed(host, capsys):
    host.set_vm("running", after_poweroff=["stopping", "stopping", "poweroff"])
    assert run_cli(host.args(run=True), capsys)[0] == 0
    observed = [step["observed"] for step in host.receipt_json()["steps"] if "observed" in step]
    assert observed == ["running", "stopping", "poweroff", "absent"]


@pytest.mark.parametrize("state", REFUSED_STATES)
def test_snapshotting_and_unknown_states_fail_closed(host, capsys, state):
    host.set_vm(state)
    code, out, err = run_cli(host.args(run=True), capsys)
    assert code == 2 and f"Lane teardown: REFUSED: {UUID}" in out
    assert f"state '{state}' is outside the teardown allow-list" in err
    assert host.mutations() == [] and host.vms()[UUID]["VMState"] == state and host.run_dir.is_dir()
    receipt = host.receipt_json()
    assert receipt["outcome"] == "refused" and receipt["steps"] == [{"observed": state}]
    assert receipt["runDirectory"] == "kept" and receipt["box"]["sha256AfterTeardown"] is None
    assert teardown.check_receipt(receipt) == []
    code, _, err = verify(host, capsys)
    assert code == 1 and "consistent but records refused" in err


@pytest.mark.parametrize("target", [NAME, "golden-appliance", "{" + UUID + "}", UUID.replace("-", ""), "", "urn:uuid:" + UUID])
def test_a_target_that_is_not_a_uuid_is_rejected_before_any_query(host, capsys, target):
    for run in (False, True):
        code, _, err = run_cli(host.args(run=run, **{"--uuid": target}), capsys)
        assert code == 2 and "lane targets are VM UUIDs only" in err
    assert host.calls() == [] and not host.receipt.exists() and host.run_dir.is_dir()


def test_an_uppercase_uuid_is_canonicalized(host, capsys):
    assert run_cli(host.args(run=True, **{"--uuid": UUID.upper()}), capsys)[0] == 0
    assert host.receipt_json()["instance"]["uuid"] == UUID


def test_two_requests_for_one_uuid_run_one_after_the_other(host, monkeypatch):
    monkeypatch.setenv("LANE_FAKE_VBOX", "poweroff-sleep")
    monkeypatch.setenv("LANE_FAKE_SLEEP", "1")
    vbox = teardown.VirtualBox(str(host.vboxmanage))
    results: dict[str, dict] = {}

    def request(label: str) -> None:
        receipt, _ = teardown.teardown(host.request(host.out / f"{label}.json"), vbox)
        results[label] = receipt

    first = threading.Thread(target=request, args=("first",))
    first.start()
    deadline = time.monotonic() + 10
    while ["controlvm", UUID, "poweroff"] not in host.calls() and time.monotonic() < deadline:
        time.sleep(0.02)
    second = threading.Thread(target=request, args=("second",))
    second.start()
    first.join()
    second.join()
    assert results["first"]["outcome"] == "removed" and results["second"]["outcome"] == "absent"
    assert results["second"]["steps"] == [{"observed": "absent"}] and results["second"]["runDirectory"] == "absent"
    calls = host.calls()
    # The second request queried nothing until the first had observed the UUID absent.
    assert calls[-2:] == [["list", "vms"], ["list", "vms"]]
    assert calls.index(["unregistervm", UUID, "--delete"]) < len(calls) - 2
    assert host.mutations() == [teardown.command_for("poweroff", UUID), teardown.command_for("delete", UUID)]


def test_a_held_lock_refuses_after_the_wait(host, capsys, monkeypatch):
    monkeypatch.setattr(teardown, "LOCK_WAIT", 0)
    descriptor = os.open(host.locks / f"{UUID}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        code, _, err = run_cli(host.args(run=True), capsys)
    finally:
        os.close(descriptor)
    assert code == 2 and "another teardown of this UUID still holds the lock" in err
    assert host.calls() == [] and not host.receipt.exists()


def test_a_symbolic_link_lock_file_is_refused(host, capsys):
    (host.locks / f"{UUID}.lock").symlink_to(host.root / "elsewhere")
    code, _, err = run_cli(host.args(run=True), capsys)
    assert code == 2 and "cannot open the teardown lock" in err
    assert not (host.root / "elsewhere").exists()


# ---------------------------------------------------------------- run directory binding


def _remove_run_dir(host: Host) -> None:
    import shutil
    shutil.rmtree(host.run_dir)


def _damage(host: Host, kind: str) -> None:
    machine_id = host.run_dir / MACHINE_ID
    if kind == "no-machine-id":
        machine_id.unlink()
    elif kind == "other-vm":
        machine_id.write_text(OTHER_UUID, encoding="utf-8")
    elif kind == "unreadable":
        machine_id.write_bytes(b"\xff\xfe")
    elif kind == "linked-vagrant-dir":
        target = host.root / "vagrant-elsewhere"
        (host.run_dir / ".vagrant").rename(target)
        (host.run_dir / ".vagrant").symlink_to(target)
    elif kind == "not-private":
        host.run_dir.chmod(0o755)
    elif kind == "linked-run-dir":
        target = host.root / "run-elsewhere"
        host.run_dir.rename(target)
        host.run_dir.symlink_to(target)
    elif kind == "a-file":
        _remove_run_dir(host)
        host.run_dir.write_text("not a directory", encoding="utf-8")


@pytest.mark.parametrize("kind, message", [
    ("no-machine-id", "has no Vagrant machine ID"),
    ("other-vm", "names a different VM"),
    ("unreadable", "cannot read the run directory's Vagrant machine ID"),
    ("linked-vagrant-dir", "has a symbolic link: .vagrant"),
    ("not-private", "mode 700 or stricter"),
    ("linked-run-dir", "is not a directory"),
    ("a-file", "is not a directory"),
])
def test_a_run_directory_that_does_not_bind_the_uuid_is_refused(host, capsys, kind, message):
    _damage(host, kind)
    for run in (False, True):
        code, _, err = run_cli(host.args(run=run), capsys)
        assert code == 2 and message in err
    assert host.calls() == [] and not host.receipt.exists() and UUID in host.vms()


def test_a_present_vm_without_a_run_directory_is_refused(host, capsys):
    _remove_run_dir(host)
    code, _, err = run_cli(host.args(run=True), capsys)
    assert code == 2 and "no run directory binds this UUID" in err
    assert host.mutations() == [] and host.receipt_json()["outcome"] == "refused"


def test_an_absent_vm_without_a_run_directory_is_reported_absent(host, capsys):
    host.set_vm(present=False)
    _remove_run_dir(host)
    code, out, _ = run_cli(host.args(run=True), capsys)
    assert code == 0 and f"Lane teardown: ABSENT: {UUID}" in out
    receipt = host.receipt_json()
    assert receipt["steps"] == [{"observed": "absent"}] and receipt["runDirectory"] == "absent"
    assert verify(host, capsys)[:2] == (0, f"Lane teardown receipt: PASS: absent: {UUID}\n")


def test_a_rerun_after_the_vm_is_gone_removes_the_run_directory(host, capsys):
    host.set_vm(present=False)
    assert run_cli(host.args(run=True), capsys)[0] == 0
    receipt = host.receipt_json()
    assert receipt["outcome"] == "absent" and receipt["runDirectory"] == "removed" and not host.run_dir.exists()
    assert host.mutations() == []


def test_a_vm_that_is_not_a_lane_instance_is_refused(host, capsys):
    host.set_vm("poweroff", name="golden-appliance")
    for run in (False, True):
        code, _, err = run_cli(host.args(run=run), capsys)
        assert code == 2 and "is not a lane instance" in err
    assert host.mutations() == [] and host.receipt_json()["outcome"] == "refused"


# ---------------------------------------------------------------- failures


def test_a_failed_poweroff_leaves_the_vm(host, capsys, monkeypatch):
    monkeypatch.setenv("LANE_FAKE_VBOX", "poweroff-fail")
    code, out, err = run_cli(host.args(run=True), capsys)
    assert code == 1 and f"Lane teardown: FAILED: {UUID}" in out and "VBoxManage controlvm failed: exit code 1" in err
    receipt = host.receipt_json()
    assert receipt["steps"] == [{"observed": "running"}, {"command": teardown.command_for("poweroff", UUID), "result": 1}]
    assert teardown.check_receipt(receipt) == [] and host.run_dir.is_dir() and UUID in host.vms()


def test_a_poweroff_that_times_out_is_recorded(host, capsys, monkeypatch):
    monkeypatch.setenv("LANE_FAKE_VBOX", "poweroff-sleep")
    monkeypatch.setenv("LANE_FAKE_SLEEP", "30")
    monkeypatch.setattr(teardown, "POWEROFF_TIMEOUT", 0.5)
    code, _, err = run_cli(host.args(run=True), capsys)
    assert code == 1 and "VBoxManage controlvm failed: timed out" in err
    assert host.receipt_json()["steps"][-1]["result"] == "timeout"
    assert teardown.check_receipt(host.receipt_json()) == []


def test_a_vm_that_does_not_stop_is_left_in_place(host, capsys, monkeypatch):
    host.set_vm("running", after_poweroff=["running"])
    monkeypatch.setattr(teardown, "POWEROFF_WAIT", 0.2)
    code, _, err = run_cli(host.args(run=True), capsys)
    assert code == 1 and "did not stop within 0.2 s" in err
    assert host.mutations() == [teardown.command_for("poweroff", UUID)] and host.run_dir.is_dir()
    assert host.receipt_json()["outcome"] == "failed"


def test_a_state_outside_the_allow_list_after_poweroff_is_refused(host, capsys):
    host.set_vm("running", after_poweroff=["saved"])
    code, _, err = run_cli(host.args(run=True), capsys)
    assert code == 1 and "state 'saved' is outside the teardown allow-list" in err
    receipt = host.receipt_json()
    assert receipt["outcome"] == "refused" and receipt["steps"][-1] == {"observed": "saved"}
    assert host.mutations() == [teardown.command_for("poweroff", UUID)]
    assert teardown.check_receipt(receipt) == []


def test_a_vm_that_disappears_after_poweroff_is_reported_absent(host, capsys):
    host.set_vm("running", after_poweroff=["absent"])
    assert run_cli(host.args(run=True), capsys)[0] == 0
    receipt = host.receipt_json()
    assert receipt["outcome"] == "absent" and receipt["steps"][-1] == {"observed": "absent"}
    assert teardown.check_receipt(receipt) == []


@pytest.mark.parametrize("mode, message", [
    ("delete-fail", "VBoxManage unregistervm failed: exit code 1"),
    ("delete-noop", "the VM is still registered after the delete"),
])
def test_a_delete_that_does_not_remove_the_vm_fails(host, capsys, monkeypatch, mode, message):
    host.set_vm("poweroff")
    monkeypatch.setenv("LANE_FAKE_VBOX", mode)
    code, _, err = run_cli(host.args(run=True), capsys)
    assert code == 1 and message in err
    receipt = host.receipt_json()
    assert receipt["outcome"] == "failed" and receipt["runDirectory"] == "kept" and host.run_dir.is_dir()
    assert teardown.check_receipt(receipt) == []


def test_a_changed_box_fails_after_removal(host, capsys):
    host.box.write_bytes(BOX_BYTES + b"!")
    code, _, err = run_cli(host.args(run=True), capsys)
    assert code == 1 and "the box file changed" in err
    receipt = host.receipt_json()
    assert receipt["outcome"] == "failed" and receipt["box"]["sha256AfterTeardown"] != BOX_SHA256
    assert UUID not in host.vms() and teardown.check_receipt(receipt) == []


@pytest.mark.parametrize("state, code", [("running", 1), ("saved", 2)])
def test_a_receipt_that_cannot_be_written_is_reported(host, capsys, monkeypatch, state, code):
    host.set_vm(state)

    def full(path, content):
        raise OSError("no space left on device")

    monkeypatch.setattr(teardown, "write_private", full)
    exit_code, _, err = run_cli(host.args(run=True), capsys)
    assert exit_code == code and "but its receipt cannot be written: no space left on device" in err
    assert not host.receipt.exists()


def test_run_directory_and_box_errors_after_removal_are_recorded(host, capsys, monkeypatch):
    def refuse(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(teardown.shutil, "rmtree", refuse)
    code, _, err = run_cli(host.args(run=True), capsys)
    assert code == 1 and "cannot remove the run directory: read-only file system" in err
    assert host.receipt_json()["runDirectory"] == "kept"


def _interrupting_rmtree(monkeypatch, error: BaseException) -> None:
    """An rmtree that deletes Vagrant's machine ID first and then stops."""
    real = teardown.shutil.rmtree

    def partial(path, *args, **kwargs):
        monkeypatch.setattr(teardown.shutil, "rmtree", real)
        (Path(path) / MACHINE_ID).unlink()
        raise error

    monkeypatch.setattr(teardown.shutil, "rmtree", partial)


@pytest.mark.parametrize("error", [OSError("device busy"), KeyboardInterrupt()])
def test_a_rerun_finishes_an_interrupted_run_directory_delete(host, capsys, monkeypatch, error):
    _interrupting_rmtree(monkeypatch, error)
    leftover = teardown.leftover_for(host.run_dir, UUID)
    if isinstance(error, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            cli.main(host.args(run=True))
        assert not host.receipt.exists()
    else:
        code, _, err = run_cli(host.args(run=True), capsys)
        assert code == 1 and "cannot remove the run directory: device busy" in err
        assert host.receipt_json()["runDirectory"] == "kept"
    assert not host.run_dir.exists() and leftover.is_dir() and not (leftover / MACHINE_ID).exists()
    assert UUID not in host.vms()
    rerun = host.out / "rerun.json"
    code, out, _ = run_cli(host.args(**{"--receipt-out": str(rerun)}), capsys)
    assert code == 0 and f"step=remove {leftover}" in out and f"step=remove {host.run_dir}" not in out
    code, out, _ = run_cli(host.args(run=True, **{"--receipt-out": str(rerun)}), capsys)
    assert code == 0 and f"Lane teardown: ABSENT: {UUID}" in out
    receipt = json.loads(rerun.read_text(encoding="utf-8"))
    assert receipt["runDirectory"] == "removed" and receipt["steps"] == [{"observed": "absent"}]
    assert not leftover.exists() and teardown.check_receipt(receipt) == []
    assert host.mutations() == [teardown.command_for("poweroff", UUID), teardown.command_for("delete", UUID)]


def test_a_stale_leftover_is_deleted_with_the_run_directory(host, capsys):
    leftover = private_dir(teardown.leftover_for(host.run_dir, UUID))
    (leftover / "stale").write_text("x", encoding="utf-8")
    assert run_cli(host.args(run=True), capsys)[0] == 0
    assert not leftover.exists() and not host.run_dir.exists()


@pytest.mark.parametrize("kind, message", [
    ("symlink", "the leftover run directory is not a directory"),
    ("file", "the leftover run directory is not a directory"),
    ("not-private", "mode 700 or stricter"),
])
def test_a_suspicious_leftover_is_not_deleted(host, capsys, kind, message):
    host.set_vm(present=False)
    _remove_run_dir(host)
    leftover = teardown.leftover_for(host.run_dir, UUID)
    target = private_dir(host.root / "keep")
    (target / "data").write_text("keep", encoding="utf-8")
    if kind == "symlink":
        leftover.symlink_to(target)
    elif kind == "file":
        leftover.write_text("keep", encoding="utf-8")
    else:
        leftover.mkdir(mode=0o755)
        leftover.chmod(0o755)
    code, _, err = run_cli(host.args(run=True), capsys)
    assert code == 2 and message in err
    assert os.path.lexists(leftover) and (target / "data").exists()
    assert host.receipt_json()["outcome"] == "failed"


def test_a_box_that_cannot_be_hashed_is_recorded(host, capsys, monkeypatch):
    def refuse(path):
        raise OSError("input/output error")

    monkeypatch.setattr(teardown, "sha256_file", refuse)
    code, _, err = run_cli(host.args(run=True), capsys)
    assert code == 1 and "cannot hash the box file: input/output error" in err


@pytest.mark.parametrize("mode, outcome, message", [
    ("list-fail", "failed", "cannot list the registered VMs"),
    ("show-fail", "failed", "cannot read the VM state"),
    ("wrong-uuid", "failed", "VirtualBox reported a different UUID"),
    ("no-state", "failed", "VirtualBox reported no VM state"),
    ("vanish-on-show", "absent", ""),
])
def test_query_problems_change_nothing(host, capsys, monkeypatch, mode, outcome, message):
    monkeypatch.setenv("LANE_FAKE_VBOX", mode)
    code, _, err = run_cli(host.args(run=True), capsys)
    assert code == (0 if outcome == "absent" else 2) and message in err
    assert host.mutations() == []
    receipt = host.receipt_json()
    assert receipt["outcome"] == outcome and teardown.check_receipt(receipt) == []


def test_query_and_command_errors_are_named(host, monkeypatch):
    vbox = teardown.VirtualBox(str(host.vboxmanage))

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args, 1)

    def missing(*args, **kwargs):
        raise OSError("no such file")

    monkeypatch.setattr(teardown.subprocess, "run", timeout)
    with pytest.raises(LaneBoxError, match="cannot list the registered VMs"):
        vbox.observe(UUID)
    assert vbox.run(["controlvm", UUID, "poweroff"], 1) == "timeout"
    monkeypatch.setattr(teardown.subprocess, "run", missing)
    with pytest.raises(LaneBoxError, match="cannot run VBoxManage: no such file"):
        vbox.run(["controlvm", UUID, "poweroff"], 1)


# ---------------------------------------------------------------- inputs


@pytest.mark.parametrize("overrides, message", [
    ({"--run-dir": "run"}, "--run-dir must be an absolute path"),
    ({"--box": "lane.box"}, "--box must be an absolute path"),
    ({"--receipt-out": "teardown.json"}, "--receipt-out must be an absolute path"),
    ({"--box-sha256": "ABC"}, "--box-sha256 must be 64 lowercase hexadecimal digits"),
    ({"--box": "/nonexistent/lane.box"}, "box file is missing"),
    ({"--run-dir": "/tmp/runs/../run"}, "--run-dir must not contain '..'"),
])
def test_input_refusals(host, capsys, overrides, message):
    code, _, err = run_cli(host.args(run=True, **overrides), capsys)
    assert code == 2 and message in err and host.calls() == []


def test_placement_refusals(host, capsys):
    host.locks.chmod(0o755)
    assert "mode 700 or stricter" in run_cli(host.args(run=True), capsys)[2]
    host.locks.chmod(0o700)
    host.out.chmod(0o755)
    assert "mode 700 or stricter" in run_cli(host.args(run=True), capsys)[2]
    host.out.chmod(0o700)
    host.receipt.write_text("{}", encoding="utf-8")
    assert "receipt output already exists" in run_cli(host.args(run=True), capsys)[2]
    host.receipt.unlink()
    Path(f"{host.receipt}.sha256").write_text("", encoding="utf-8")
    assert "receipt output already exists" in run_cli(host.args(run=True), capsys)[2]
    Path(f"{host.receipt}.sha256").unlink()
    inner = private_dir(host.run_dir / "inner")
    (inner / "lane.box").write_bytes(BOX_BYTES)
    leftover = private_dir(teardown.leftover_for(host.run_dir, UUID))
    (leftover / "lane.box").write_bytes(BOX_BYTES)
    for overrides, what in (
        ({"--box": str(inner / "lane.box")}, "box file"),
        ({"--box": str(leftover / "lane.box")}, "box file"),
        ({"--receipt-out": str(inner / "teardown.json")}, "receipt"),
        ({"--lock-dir": str(inner)}, "lock directory"),
    ):
        code, _, err = run_cli(host.args(run=True, **overrides), capsys)
        assert code == 2 and f"the {what} must be outside the run directory" in err
    assert host.calls() == []


def test_missing_vboxmanage_exits_127(host, capsys):
    code, _, err = run_cli(host.args(run=True, **{"--vboxmanage": str(host.root / "no-such-vboxmanage")}), capsys)
    assert code == 127 and "VBoxManage is missing" in err


def test_the_default_lock_directory_is_private_under_xdg_runtime_dir(host, capsys, monkeypatch):
    runtime = private_dir(host.root / "runtime")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    assert run_cli(host.args(run=True, **{"--lock-dir": None}), capsys)[0] == 0
    assert oct((runtime / cli.LOCK_DIRECTORY).stat().st_mode & 0o777) == "0o700"
    assert (runtime / cli.LOCK_DIRECTORY / f"{UUID}.lock").is_file()
    monkeypatch.setenv("XDG_RUNTIME_DIR", "relative")
    code, _, err = run_cli(host.args(**{"--lock-dir": None}), capsys)
    assert code == 2 and "pass --lock-dir" in err
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    assert run_cli(host.args(**{"--lock-dir": None}), capsys)[0] == 2


def test_invoke_lane_entry_point(host, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["Invoke-Lane.py", *host.args()])
    with pytest.raises(SystemExit) as raised:
        runpy.run_path(str(SCRIPTS / "Invoke-Lane.py"), run_name="__main__")
    assert raised.value.code == 0 and "Lane teardown: PLAN" in capsys.readouterr().out


# ---------------------------------------------------------------- receipt verifier


def test_a_receipt_that_claims_removal_without_the_absence_observation_fails(host, capsys):
    receipt = removed_receipt(host, capsys)
    for steps in (receipt["steps"][:-1], [*receipt["steps"][:-1], {"observed": "poweroff"}]):
        host.rewrite_receipt({**receipt, "steps": steps})
        code, _, err = verify(host, capsys)
        assert code == 2 and "claims removal without observing the UUID absent" in err


def test_every_accepted_receipt_binds_the_uuid_states_and_absence(host, capsys):
    receipt = removed_receipt(host, capsys)
    assert receipt["instance"]["uuid"] == UUID
    assert [step for step in receipt["steps"] if "observed" in step][-1] == {"observed": "absent"}
    assert {"observed": "running"} in receipt["steps"]


def test_the_verifier_checks_the_hash_file_and_the_expected_values(host, capsys):
    removed_receipt(host, capsys)
    assert "does not match" in verify(host, capsys, "--uuid", OTHER_UUID)[2]
    assert "the expected box digest does not match" in verify(host, capsys, "--box-sha256", "0" * 64)[2]
    assert "lane targets are VM UUIDs only" in verify(host, capsys, "--uuid", NAME)[2]
    host.receipt.write_text(host.receipt.read_text(encoding="utf-8").replace("removed", "absent", 1), encoding="utf-8")
    assert "hash mismatch" in verify(host, capsys)[2]
    Path(f"{host.receipt}.sha256").unlink()
    assert "hash file is missing" in verify(host, capsys)[2]


def _break(receipt: dict, how: str) -> dict:
    value = copy.deepcopy(receipt)
    steps = value["steps"]
    poweroff, delete = teardown.command_for("poweroff", UUID), teardown.command_for("delete", UUID)
    if how == "extra-field":
        value["note"] = "x"
    elif how == "schema":
        value["schema"] = "projectbuddy/lane-teardown@2"
    elif how == "credential":
        value["instance"]["token"] = "x"
    elif how == "uuid":
        value["instance"]["uuid"] = UUID.upper()
    elif how == "name-type":
        value["instance"]["name"] = 7
    elif how == "allow-list":
        value["allowList"]["delete"].append("saved")
    elif how == "steps-type":
        value["steps"] = {"observed": "absent"}
    elif how == "malformed-step":
        steps.insert(1, {"observed": "running", "note": "x"})
    elif how == "step-after-absence":
        steps.append({"observed": "absent"})
    elif how == "other-uuid":
        steps[1] = {"command": ["controlvm", OTHER_UUID, "poweroff"], "result": 0}
    elif how == "delete-while-running":
        value["steps"] = [{"observed": "running"}, {"command": delete, "result": 0}, {"observed": "absent"}]
    elif how == "command-after-command":
        value["steps"] = [{"observed": "running"}, {"command": poweroff, "result": 0},
                          {"command": delete, "result": 0}, {"observed": "absent"}]
    elif how == "bool-result":
        steps[1] = {"command": poweroff, "result": False}
    elif how == "float-result":
        steps[1] = {"command": poweroff, "result": 0.0}
    elif how == "steps-after-failure":
        steps[1] = {"command": poweroff, "result": 1}
    elif how == "not-a-lane":
        value["instance"]["name"] = "golden-appliance"
    elif how == "box-digest":
        value["box"]["expectedSha256"] = "not-a-digest"
    elif how == "run-directory":
        value["runDirectory"] = "gone"
    elif how == "failures-type":
        value["failures"] = "none"
    elif how == "outcome":
        value["outcome"] = "done"
    elif how == "removal-with-failures":
        value["failures"] = ["something"]
    elif how == "absent-after-delete":
        value["outcome"] = "absent"
    elif how == "run-directory-kept":
        value["runDirectory"] = "kept"
    elif how == "box-changed":
        value["box"]["sha256AfterTeardown"] = "0" * 64
    elif how == "failed-without-failure":
        value["outcome"] = "failed"
    elif how == "refused-but-absent":
        value.update(outcome="refused", failures=["refused"])
    return value


@pytest.mark.parametrize("how, message", [
    ("extra-field", "unexpected or missing fields"),
    ("schema", "unexpected schema"),
    ("credential", "holds a credential field"),
    ("uuid", "not a canonical UUID"),
    ("name-type", "the instance name is not a string"),
    ("allow-list", "the allow-list differs from REQ-102"),
    ("steps-type", "the steps are not a list"),
    ("malformed-step", "step 1 is malformed"),
    ("step-after-absence", "follows the absence observation"),
    ("other-uuid", "step 1 is not a poweroff or delete of the instance UUID"),
    ("delete-while-running", "step 1 runs delete without an observed state that allows it"),
    ("command-after-command", "step 2 runs delete without an observed state that allows it"),
    ("bool-result", "step 1 has a result that is neither an exit code nor"),
    ("float-result", "step 1 has a result that is neither an exit code nor"),
    ("steps-after-failure", "step 1 failed, but steps follow it"),
    ("not-a-lane", "a command was run against a VM that is not a lane instance"),
    ("box-digest", "the expected box digest is not a SHA-256"),
    ("run-directory", "unknown run directory state"),
    ("failures-type", "failures is not a list of strings"),
    ("outcome", "unknown outcome"),
    ("removal-with-failures", "a removal claim carries failures"),
    ("absent-after-delete", "the outcome does not match the delete commands"),
    ("run-directory-kept", "the run directory was not removed"),
    ("box-changed", "the box digest after teardown differs"),
    ("failed-without-failure", "a failed receipt names no failure"),
    ("refused-but-absent", "a refusal must end with an observation of a VM that is still present"),
])
def test_receipt_rules(host, capsys, how, message):
    receipt = removed_receipt(host, capsys)
    assert teardown.check_receipt(receipt) == []
    errors = teardown.check_receipt(_break(receipt, how))
    assert any(message in error for error in errors), errors
    host.rewrite_receipt(_break(receipt, how))
    assert verify(host, capsys)[0] == 2


def test_a_failed_receipt_with_no_steps_is_consistent(host, capsys, monkeypatch):
    monkeypatch.setenv("LANE_FAKE_VBOX", "list-fail")
    run_cli(host.args(run=True), capsys)
    receipt = host.receipt_json()
    assert receipt["steps"] == [] and teardown.check_receipt(receipt) == []
    assert verify(host, capsys)[0] == 1
