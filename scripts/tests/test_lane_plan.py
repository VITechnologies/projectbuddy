"""Lane plan (VITechnologies/projectbuddy#185, REQ-090 to REQ-093, REQ-101, TEST-103, TEST-105): dry-run checks."""
from __future__ import annotations

import ast
import hashlib
import http.server
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import threading
import types
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import test_lane_box as box_tests  # noqa: E402
import test_lane_teardown as teardown_tests  # noqa: E402
from lane import cli, declaration as lane_declaration, plan  # noqa: E402
from lane.common import INSTANCE_NAME, Refusal, write_new_with_digest  # noqa: E402
from lane_box import cli as box_cli, common, readiness  # noqa: E402
from lane_box.common import LaneBoxError  # noqa: E402

REPOSITORY = SCRIPTS.parent
EXAMPLE_DECLARATION = REPOSITORY / "vm" / "ubuntu-lane" / "lane-workload.example.json"
RECORDED_UUID = teardown_tests.UUID
RECORDED_NAME = teardown_tests.NAME
FIXED_NAME = "pb-lane-aaaaaaaaaaaa"
CANARY = "lane-canary-0f9e8d7c6b5a"
TIB = 1024 ** 4
GIT_ENV = {
    "GIT_AUTHOR_NAME": "lane", "GIT_AUTHOR_EMAIL": "lane@example.invalid", "GIT_COMMITTER_NAME": "lane",
    "GIT_COMMITTER_EMAIL": "lane@example.invalid", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}
READ_ONLY_CALLS = (["VBoxManage", "--version"], ["VBoxManage", "list", "vms"],
                   ["VBoxManage", "list", "systemproperties"], ["vagrant", "--version"])

HOST_STUB = """
import json, os, sys
path = os.environ["LANE_PLAN_HOST"]
state = json.load(open(path, encoding="utf-8"))
tool = os.path.basename(sys.argv[0])
args = sys.argv[1:]
with open(path + ".log", "a", encoding="utf-8") as log:
    log.write(json.dumps([tool, *args]) + "\\n")
modes = state["modes"]
if tool == "vagrant":
    if args == ["--version"] and "vagrant-fail" not in modes:
        print(state["vagrant"])
        sys.exit(0)
    sys.exit(2)
if args == ["--version"] and "version-fail" not in modes:
    print(state["version"])
elif args == ["list", "vms"] and "list-fail" not in modes:
    for uuid, name in state["vms"].items():
        print('"%s" {%s}' % (name, uuid))
    print("not a VM line")
elif args == ["list", "systemproperties"] and "properties-fail" not in modes:
    print("API version:                     7_2")
    if state["folder"] is not None:
        print("Default machine folder:          " + state["folder"])
    print("Raw-mode Supported:              no")
else:
    sys.exit(2)
"""


def private_dir(path: Path) -> Path:
    return box_tests.private_dir(path)


def git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(["git", "-C", str(repository), *arguments], capture_output=True, text=True, check=True,
                            env=dict(os.environ, **GIT_ENV))
    return result.stdout.strip()


def make_repository(path: Path, files: dict[str, str], symlinks: dict[str, str] | None = None,
                    gitlinks: tuple[str, ...] = ()) -> str:
    private_dir(path)
    git(path, "init", "-q")
    for name, text in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text, encoding="utf-8")
    for name, target in (symlinks or {}).items():
        os.symlink(target, path / name)
    git(path, "add", "-A")
    for name in gitlinks:
        git(path, "update-index", "--add", "--cacheinfo", f"160000,{'1' * 40},{name}")
    git(path, "commit", "-q", "-m", "candidate")
    return git(path, "rev-parse", "HEAD")


def rehash(path: Path) -> None:
    Path(f"{path}.sha256").write_text(common.hash_line(path), encoding="utf-8")


@dataclass
class Materials:
    box: Path
    receipt: Path
    base_receipt: Path
    readiness_receipt: Path
    removed: Path
    absent: Path


@pytest.fixture(scope="module")
def materials(tmp_path_factory: pytest.TempPathFactory) -> Materials:
    """One real lane box with its receipt, and real teardown receipts: built once, copied per test."""
    root = tmp_path_factory.mktemp("materials")
    with pytest.MonkeyPatch.context() as patch:
        built = box_tests.Host(root / "box", patch)
        assert box_cli.main(built.package_args(run=True)) == 0
        removed = teardown_tests.Host(root / "removed", patch)
        assert cli.main(removed.args(run=True)) == 0
        absent = teardown_tests.Host(root / "absent", patch)
        absent.set_vm(present=False)
        assert cli.main(absent.args(run=True)) == 0
    assert json.loads(absent.receipt.read_text(encoding="utf-8"))["instance"]["name"] is None
    return Materials(box=built.box, receipt=built.receipt,
                     base_receipt=built.evidence / "base-bootstrap-receipt.json",
                     readiness_receipt=built.evidence / readiness.READINESS_FILENAME,
                     removed=removed.receipt, absent=absent.receipt)


class Lane:
    """A host layout whose plan is ready: the box inputs, two git repositories, and stub VirtualBox and Vagrant."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, materials: Materials) -> None:
        self.monkeypatch = monkeypatch
        self.root = private_dir(tmp_path / "lane")
        inputs = private_dir(self.root / "inputs")
        for source in (materials.box, materials.receipt, materials.base_receipt, materials.readiness_receipt):
            shutil.copy2(source, inputs / source.name)
        self.box = inputs / materials.box.name
        self.receipt = inputs / materials.receipt.name
        self.base_receipt = inputs / materials.base_receipt.name
        self.readiness_receipt = inputs / materials.readiness_receipt.name
        self.materials = materials
        self.work = private_dir(self.root / "work")
        self.evidence = private_dir(self.root / "evidence")
        self.records = private_dir(self.root / "records")
        self.plans = private_dir(self.root / "plans")
        self.plan_out = self.plans / "lane-plan.json"
        self.declaration = self.root / "declaration.json"
        self.candidate = self.root / "candidate"
        self.commit = make_repository(
            self.candidate,
            {"package.json": "{}\n", "package-lock.json": "{}\n", "src/index.js": "\n", "README.md": "\n"},
            symlinks={"linked": "src"}, gitlinks=("vendor/sub",),
        )
        self.engine = self.root / "engine"
        make_repository(self.engine, {"README.md": "engine\n"})
        monkeypatch.setattr(cli, "ENGINE", self.engine)
        bin_dir = private_dir(self.root / "bin")
        self.vboxmanage = box_tests.Host._stub(bin_dir / "VBoxManage", HOST_STUB)
        self.vagrant = box_tests.Host._stub(bin_dir / "vagrant", HOST_STUB)
        self.machine_folder = private_dir(self.root / "VirtualBox VMs")
        self.host_state = self.root / "host-state.json"
        self.set_host()
        monkeypatch.setenv("LANE_PLAN_HOST", str(self.host_state))
        self.free = TIB
        monkeypatch.setattr(plan, "shutil", types.SimpleNamespace(
            disk_usage=lambda path: types.SimpleNamespace(free=self.free)))
        self.declare()

    def set_host(self, vms: dict[str, str] | None = None, modes: tuple[str, ...] = (), folder: str | None = "",
                 vagrant: str = "Vagrant 2.4.9", version: str = "7.2.6r172322") -> None:
        value = {"vms": vms or {}, "modes": list(modes), "vagrant": vagrant, "version": version,
                 "folder": str(self.machine_folder) if folder == "" else folder}
        self.host_state.write_text(json.dumps(value), encoding="utf-8")

    def calls(self) -> list[list[str]]:
        log = Path(f"{self.host_state}.log")
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []

    def data(self, **changes: object) -> dict:
        value = json.loads(EXAMPLE_DECLARATION.read_text(encoding="utf-8"))
        value["commit"] = self.commit
        value.update(changes)
        return value

    def declare(self, **changes: object) -> None:
        self.declaration.write_text(json.dumps(self.data(**changes), indent=2), encoding="utf-8")

    def record(self, source: Path, name: str) -> Path:
        target = self.records / name
        shutil.copyfile(source, target)
        rehash(target)
        return target

    def args(self, **overrides: str | None) -> list[str]:
        values = {
            "--declaration": str(self.declaration), "--candidate-repo": str(self.candidate), "--box": str(self.box),
            "--box-receipt": str(self.receipt), "--profile": str(box_tests.EXAMPLE),
            "--base-receipt": str(self.base_receipt), "--readiness-receipt": str(self.readiness_receipt),
            "--work-dir": str(self.work), "--evidence-dir": str(self.evidence), "--records-dir": str(self.records),
            "--plan-out": str(self.plan_out), "--vboxmanage": str(self.vboxmanage), "--vagrant": str(self.vagrant),
        }
        values.update(overrides)
        argv = ["plan"]
        for key, value in values.items():
            if value is not None:
                argv += [key, value]
        return argv

    def run(self, capsys, **overrides: str | None) -> tuple[int, dict | None, str, str]:
        code = cli.main(self.args(**overrides))
        captured = capsys.readouterr()
        value = json.loads(self.plan_out.read_text(encoding="utf-8")) if self.plan_out.exists() else None
        return code, value, captured.out, captured.err


@pytest.fixture
def lane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, materials: Materials) -> Lane:
    return Lane(tmp_path, monkeypatch, materials)


def refused_rules(lane: Lane, capsys, **overrides: str | None) -> dict[str, list[str]]:
    code, value, out, err = lane.run(capsys, **overrides)
    assert code == 2 and value is not None and value["outcome"] == "refused", (code, out, err)
    rules: dict[str, list[str]] = {}
    for refusal in value["refusals"]:
        rules.setdefault(refusal["rule"], []).append(refusal["detail"])
        assert f"Lane plan: REFUSED: {refusal['rule']}: {refusal['detail']}" in err
    assert f"Lane plan: REFUSED: {value['instance']['name']}: {lane.plan_out}" in out
    assert stat.S_IMODE(lane.plan_out.stat().st_mode) == 0o600
    common.verify_hash_file(Path(f"{lane.plan_out}.sha256"), lane.plan_out)
    return rules


# ---------------------------------------------------------------- the ready plan


def test_a_ready_plan_names_every_step_and_changes_nothing(lane, capsys):
    code, value, out, err = lane.run(capsys)
    assert code == 0, err
    assert value["outcome"] == "ready" and value["refusals"] == []
    assert set(value) == set(plan.PLAN_FIELDS) and value["schema"] == plan.PLAN_SCHEMA
    name = value["instance"]["name"]
    assert INSTANCE_NAME.fullmatch(name)
    assert out == f"Lane plan: READY: {name}: {lane.plan_out}\n" and err == ""
    assert stat.S_IMODE(lane.plan_out.stat().st_mode) == 0o600
    common.verify_hash_file(Path(f"{lane.plan_out}.sha256"), lane.plan_out)
    assert common.forbidden_keys(value) == []
    # Only read-only queries ran: nothing was added, started, or changed.
    assert sorted(lane.calls()) == sorted(READ_ONLY_CALLS)
    assert sorted(os.listdir(lane.work)) == [] and sorted(os.listdir(lane.evidence)) == []
    receipt = json.loads(lane.receipt.read_text(encoding="utf-8"))
    assert value["box"] == {
        "path": str(lane.box), "sha256": receipt["box"]["sha256"], "receiptSha256": common.sha256_file(lane.receipt),
        "extractedBytes": plan.extracted_bytes(lane.box), "profile": receipt["profile"],
        "sshHostKey": receipt["sshHostKey"],
    }
    assert value["hardware"] == box_tests.example()["virtualBox"]
    assert value["engine"] == {"path": str(lane.engine), "commit": git(lane.engine, "rev-parse", "HEAD")}
    assert value["candidate"] == {"repository": str(lane.candidate), "commit": lane.commit}
    assert value["declaration"] == {"path": str(lane.declaration),
                                    "sha256": lane_declaration.canonical_sha256(lane.data())}
    assert value["instance"] == {"name": name, "runDirectory": str(lane.work / name),
                                 "evidenceDirectory": str(lane.evidence / name)}
    assert value["host"]["virtualBox"] == "7.2.6r172322" and value["host"]["vagrant"] == "2.4.9"
    assert value["host"]["machineFolder"] == str(lane.machine_folder)
    # The work directory and the machine folder share a file system, so one instance needs both amounts there.
    [disk] = value["host"]["disks"]
    assert disk["requiredBytes"] == (lane.box.stat().st_size + value["box"]["extractedBytes"]
                                     + value["hardware"]["diskGb"] * plan.GIB)
    assert disk["freeBytes"] == TIB
    assert value["earlierInstances"] == []


def test_the_generated_vagrantfile_pins_the_instance_hardware_and_one_loopback_forward(lane, capsys):
    _, value, _, _ = lane.run(capsys)
    name, port = value["instance"]["name"], value["network"]["sshForward"]["hostPort"]
    hardware = value["hardware"]
    text = value["vagrantfile"]["text"]
    assert value["vagrantfile"]["sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert text == plan.render_vagrantfile(name, port, hardware)
    for line in (
        f'config.vm.box = "{name}"', "config.vm.box_check_update = false", 'config.vm.communicator = "none"',
        'config.vm.synced_folder ".", "/vagrant", disabled: true',
        f'config.vm.network "forwarded_port", guest: 22, host: {port}, host_ip: "127.0.0.1", id: "ssh", '
        "auto_correct: false",
        f'vb.name = "{name}"', "vb.gui = false", "vb.linked_clone = false", "vb.destroy_unused_network_interfaces = false",
        f"vb.memory = {hardware['memoryMb']}", f"vb.cpus = {hardware['cpus']}",
        f'vb.customize ["modifyvm", :id, "--vram", "{hardware["vramMb"]}", "--nat-localhostreachable1", "off"]',
    ):
        assert f"\n  {line}\n" in text or f"\n    {line}\n" in text, line
    assert text.count("forwarded_port") == 1
    assert value["network"] == {"adapter": 1, "mode": "nat", "localhostReachable": False, "linkOffDuring": ["build"],
                                "sshForward": {"hostIp": "127.0.0.1", "hostPort": port, "guestPort": 22,
                                               "autoCorrect": False}}


def test_the_steps_follow_the_run_sequence_and_carry_secret_names_only(lane, capsys, monkeypatch):
    monkeypatch.setenv("LANE_TEST_SECRET", CANARY)
    lane.declare(secrets=["LANE_TEST_SECRET"], workingDirectory="src", timeoutSeconds=600)
    code, value, out, err = lane.run(capsys)
    assert code == 0, err
    assert CANARY not in lane.plan_out.read_text(encoding="utf-8") + out + err
    steps = value["steps"]
    assert [step["name"] for step in steps] == [
        "box-add", "up", "verify-instance", "connect", "check-runner-units", "probe-node", "probe-npm", "probe-git",
        "probe-ca-certificates", "transfer-candidate", "workload", "network-off", "verify-network-off",
        "await-workload", "network-on", "collect-outputs", "write-evidence", "teardown",
    ]
    assert all(set(step) == set(plan.STEP_FIELDS) for step in steps)
    phases = [plan.PHASES.index(step["phase"]) for step in steps]
    assert phases == sorted(phases) and "fetch" not in {step["phase"] for step in steps}
    by_name = {step["name"]: step for step in steps}
    assert [name for name, step in by_name.items() if step["secrets"]] == ["workload"]
    assert by_name["workload"] == {
        "name": "workload", "phase": "build", "runs": "guest", "argv": ["npm", "run", "package"], "cwd": "src",
        "environment": {}, "secrets": ["LANE_TEST_SECRET"], "timeoutSeconds": 600,
    }
    assert by_name["await-workload"]["timeoutSeconds"] == 600 + plan.HOST_DEADLINE_GRACE
    assert all(step["argv"] is None for step in steps if step["runs"] == "engine")
    name, run_dir = value["instance"]["name"], value["instance"]["runDirectory"]
    sha256 = value["box"]["sha256"]
    assert by_name["box-add"]["argv"] == [
        str(lane.vagrant), "box", "add", "--name", name, "--provider", "virtualbox", "--checksum", sha256,
        "--checksum-type", "sha256", str(lane.box)]
    for step in (by_name["box-add"], by_name["up"]):
        assert step["cwd"] == run_dir
        assert step["environment"] == {"VAGRANT_HOME": f"{run_dir}/vagrant-home", "TMPDIR": f"{run_dir}/tmp"}
    assert by_name["network-off"]["argv"] == [str(lane.vboxmanage), "controlvm", "{uuid}", "setlinkstate1", "off"]
    assert by_name["network-on"]["argv"] == [str(lane.vboxmanage), "controlvm", "{uuid}", "setlinkstate1", "on"]
    assert by_name["probe-node"]["argv"] == ["node", "--version"] and by_name["probe-node"]["runs"] == "guest"
    # Every VirtualBox command after `vagrant up` names the instance by its verified UUID.
    assert all("{uuid}" in step["argv"] for step in steps[2:] if step["runs"] == "host")
    teardown_argv = by_name["teardown"]["argv"]
    assert teardown_argv[:2] == [sys.executable, str(lane.engine / "scripts" / "Invoke-Lane.py")]
    parsed = cli.build_parser().parse_args(teardown_argv[2:])
    assert (parsed.command, parsed.uuid, parsed.run, parsed.box_sha256) == ("teardown", "{uuid}", True, sha256)
    assert parsed.run_dir == Path(run_dir) and parsed.box == lane.box
    assert parsed.receipt_out == lane.records / f"{name}{plan.RECORD_SUFFIX}"


def test_earlier_records_are_listed_and_a_recorded_name_is_not_reused(lane, capsys, monkeypatch):
    lane.record(lane.materials.removed, f"{RECORDED_NAME}{plan.RECORD_SUFFIX}")
    lane.record(lane.materials.absent, f"unnamed{plan.RECORD_SUFFIX}")
    code, value, _, err = lane.run(capsys)
    assert code == 0, err
    assert value["earlierInstances"] == [RECORDED_UUID]


def test_names_are_random_and_new_each_plan():
    names = {plan.new_instance_name() for _ in range(64)}
    assert len(names) == 64 and all(INSTANCE_NAME.fullmatch(name) for name in names)


# ---------------------------------------------------------------- refusals


@dataclass(frozen=True)
class Case:
    id: str
    rules: frozenset[str]
    arrange: Callable[[Lane], None]
    detail: str = ""


def declared(**changes: object) -> Callable[[Lane], None]:
    return lambda lane: lane.declare(**changes)


def written(text: str | bytes) -> Callable[[Lane], None]:
    def arrange(lane: Lane) -> None:
        if isinstance(text, bytes):
            lane.declaration.write_bytes(text)
        else:
            lane.declaration.write_text(text, encoding="utf-8")
    return arrange


def replaced(old: str, new: str) -> Callable[[Lane], None]:
    def arrange(lane: Lane) -> None:
        lane.declaration.write_text(json.dumps(lane.data()).replace(old, new), encoding="utf-8")
    return arrange


def without(field: str) -> Callable[[Lane], None]:
    def arrange(lane: Lane) -> None:
        value = lane.data()
        del value[field]
        lane.declaration.write_text(json.dumps(value), encoding="utf-8")
    return arrange


def toolchain(change: Callable[[list[dict]], list[dict]]) -> Callable[[Lane], None]:
    return lambda lane: lane.declare(toolchain=change(lane.data()["toolchain"]))


def probe_expected(entries: list[dict]) -> list[dict]:
    entries[0]["probe"]["expected"] = "22.0.0"
    return entries


def two_groups(entries: list[dict]) -> list[dict]:
    entries[0]["probe"]["pattern"] = "v(\\d+)\\.(\\S+)"
    return entries


def extra_tool(entries: list[dict]) -> list[dict]:
    return entries + [{"name": "make", "probe": {"argv": ["make", "--version"], "pattern": "Make (\\S+)",
                                                  "expected": "4.3"}}]


def tampered_receipt(lane: Lane) -> None:
    value = json.loads(lane.receipt.read_text(encoding="utf-8"))
    value["sshHostKey"]["fingerprint"] = "SHA256:" + "B" * 43
    lane.receipt.write_text(json.dumps(value), encoding="utf-8")


def receipt_box_string(lane: Lane) -> None:
    value = json.loads(lane.receipt.read_text(encoding="utf-8"))
    value["box"] = value["box"]["sha256"]
    lane.receipt.write_text(json.dumps(value), encoding="utf-8")


def changed_profile(lane: Lane) -> None:
    value = box_tests.example()
    value["virtualBox"]["cpus"] = 2
    profile = box_tests.write_json(lane.root / "profile.json", value)
    lane.args = (lambda args: lambda **overrides: args(**{"--profile": str(profile), **overrides}))(lane.args)


def appended_box(lane: Lane) -> None:
    with lane.box.open("ab") as stream:
        stream.write(b"\0")


def option(name: str, value: Callable[[Lane], str]) -> Callable[[Lane], None]:
    def arrange(lane: Lane) -> None:
        lane.args = (lambda args: lambda **overrides: args(**{name: value(lane), **overrides}))(lane.args)
    return arrange


def dirty_engine(lane: Lane) -> None:
    (lane.engine / "README.md").write_text("changed\n", encoding="utf-8")


def not_a_repository(attribute: str) -> Callable[[Lane], None]:
    def arrange(lane: Lane) -> None:
        empty = private_dir(lane.root / f"empty-{attribute}")
        if attribute == "engine":
            lane.monkeypatch.setattr(cli, "ENGINE", empty)
        else:
            setattr(lane, attribute, empty)
    return arrange


def tagged_commit(lane: Lane) -> None:
    """Declare an annotated tag's own SHA, which git would follow to the commit it tags."""
    git(lane.candidate, "tag", "-a", "v1", "-m", "candidate")
    lane.declare(commit=git(lane.candidate, "rev-parse", "v1"))


def broken_record(lane: Lane) -> None:
    target = lane.record(lane.materials.removed, f"broken{plan.RECORD_SUFFIX}")
    value = json.loads(target.read_text(encoding="utf-8"))
    value["outcome"] = "refused"
    target.write_text(json.dumps(value), encoding="utf-8")
    rehash(target)


def unhashed_record(lane: Lane) -> None:
    target = lane.record(lane.materials.removed, f"unhashed{plan.RECORD_SUFFIX}")
    Path(f"{target}.sha256").unlink()


def reused(lane: Lane) -> None:
    lane.record(lane.materials.removed, f"{RECORDED_NAME}{plan.RECORD_SUFFIX}")
    lane.set_host(vms={RECORDED_UUID.upper(): "someone-else"})


def fixed_name(arrange: Callable[[Lane], None]) -> Callable[[Lane], None]:
    def wrapped(lane: Lane) -> None:
        lane.monkeypatch.setattr(plan, "new_instance_name", lambda: FIXED_NAME)
        arrange(lane)
    return wrapped


def recorded_name(lane: Lane) -> None:
    lane.monkeypatch.setattr(plan, "new_instance_name", lambda: RECORDED_NAME)
    lane.record(lane.materials.removed, f"earlier{plan.RECORD_SUFFIX}")


def host(**settings: object) -> Callable[[Lane], None]:
    return lambda lane: lane.set_host(**settings)


def little_disk(lane: Lane) -> None:
    lane.free = 1024


def taken_port(lane: Lane) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    lane.monkeypatch.setattr(lane, "listener", listener, raising=False)
    option("--ssh-port", lambda _: str(listener.getsockname()[1]))(lane)


def rules(*names: str) -> frozenset[str]:
    return frozenset(names)


CASES = [
    Case("not-json", rules("declaration-json"), written("{")),
    Case("not-utf-8", rules("declaration-json"), written(b"\xff{}")),
    Case("missing-file", rules("declaration-json"), lambda lane: lane.declaration.unlink()),
    Case("duplicate-key", rules("declaration-json"), replaced('"argv"', '"argv": [], "argv"')),
    Case("fraction", rules("declaration-json"), replaced('"timeoutSeconds": 1800', '"timeoutSeconds": 1800.0')),
    Case("exponent", rules("declaration-json"), replaced('"timeoutSeconds": 1800', '"timeoutSeconds": 18e2')),
    Case("nan", rules("declaration-json"), replaced('"timeoutSeconds": 1800', '"timeoutSeconds": NaN')),
    Case("integer-too-long", rules("declaration-json"),
         replaced('"timeoutSeconds": 1800', '"timeoutSeconds": ' + "1" * 5000)),
    Case("null", rules("declaration-json"), declared(secrets=None)),
    Case("unsafe-integer", rules("declaration-json"), declared(timeoutSeconds=2 ** 53)),
    Case("lone-surrogate", rules("declaration-json"), replaced('"package"]', '"\\ud800"]')),
    Case("too-deep", rules("declaration-json"), written("[" * 100000 + "]" * 100000)),
    Case("not-an-object", rules("declaration-json"), written("[]")),
    Case("unknown-field", rules("declaration-fields"), declared(fetch=[["npm", "ci"]])),
    Case("missing-field", rules("declaration-fields", "secret-name"), without("secrets")),
    Case("other-schema", rules("declaration-fields"), declared(schema="projectbuddy/lane-workload@2")),
    Case("branch", rules("commit"), declared(commit="main")),
    Case("short-sha", rules("commit"), lambda lane: lane.declare(commit=lane.commit[:12])),
    Case("upper-sha", rules("commit"), lambda lane: lane.declare(commit=lane.commit.upper())),
    Case("argv-string", rules("argv"), declared(argv="npm run package")),
    Case("argv-empty", rules("argv"), declared(argv=[])),
    Case("argv-empty-word", rules("argv"), declared(argv=["npm", ""])),
    Case("argv-number", rules("argv"), declared(argv=["sleep", 1])),
    Case("cwd-absolute", rules("working-directory"), declared(workingDirectory="/src")),
    Case("cwd-parent", rules("working-directory"), declared(workingDirectory="../src")),
    Case("cwd-not-normal", rules("working-directory"), declared(workingDirectory="./src")),
    Case("cwd-trailing-slash", rules("working-directory"), declared(workingDirectory="src/")),
    Case("cwd-backslash", rules("working-directory"), declared(workingDirectory="src\\lib")),
    Case("cwd-empty", rules("working-directory"), declared(workingDirectory="")),
    Case("timeout-zero", rules("timeout"), declared(timeoutSeconds=0)),
    Case("timeout-negative", rules("timeout"), declared(timeoutSeconds=-5)),
    Case("timeout-bool", rules("timeout"), declared(timeoutSeconds=True)),
    Case("timeout-string", rules("timeout"), declared(timeoutSeconds="60")),
    Case("input-absolute", rules("input-path"), declared(inputs=["/etc/passwd"])),
    Case("input-parent", rules("input-path"), declared(inputs=["src/../../x"])),
    Case("input-dot", rules("input-path"), declared(inputs=["src/./index.js"])),
    Case("input-root", rules("input-path"), declared(inputs=["."])),
    Case("input-nul", rules("input-path"), declared(inputs=["src\0x"])),
    Case("input-twice", rules("input-path"), declared(inputs=["package.json", "package.json"])),
    Case("inputs-string", rules("input-path"), declared(inputs="package.json")),
    Case("output-absolute", rules("output-path"), declared(outputs=["/tmp/out.tgz"])),
    Case("output-parent", rules("output-path"), declared(outputs=["../out.tgz"])),
    Case("output-twice", rules("output-path"), declared(outputs=["a.tgz", "a.tgz"])),
    Case("outputs-number", rules("output-path"), declared(outputs=[1])),
    *[Case(f"output-{name}", rules("output-forbidden"), declared(outputs=["ok.tgz", name]))
      for name in ("disk.VDI", "vm/disk.vmdk", "a.vhd", "a.VHDX", "state.sav", "vm.vbox", "known_hosts",
                   ".ssh/KNOWN_HOSTS")],
    Case("secret-digit", rules("secret-name"), declared(secrets=["1TOKEN"])),
    Case("secret-dash", rules("secret-name"), declared(secrets=["VSCE-PAT"])),
    Case("secret-twice", rules("secret-name"), declared(secrets=["A", "A"])),
    Case("toolchain-empty", rules("toolchain"), declared(toolchain=[])),
    Case("toolchain-string", rules("toolchain"), declared(toolchain="node")),
    Case("toolchain-entry", rules("toolchain"), declared(toolchain=[1])),
    Case("toolchain-field", rules("toolchain"), toolchain(lambda entries: [dict(entries[0], version="24.19.0")])),
    Case("toolchain-name", rules("toolchain"), toolchain(lambda entries: [dict(entries[0], name="Node")])),
    Case("toolchain-groups", rules("toolchain"), toolchain(two_groups)),
    Case("toolchain-twice", rules("toolchain"), toolchain(lambda entries: entries + entries[:1])),
    Case("contract-version", rules("toolchain-contract"), toolchain(probe_expected)),
    Case("contract-missing", rules("toolchain-contract"), toolchain(lambda entries: entries[1:])),
    Case("contract-extra", rules("toolchain-contract"), toolchain(extra_tool)),
    Case("box-digest", rules("box-digest"), appended_box),
    Case("receipt-without-box", rules("box-digest"), receipt_box_string),
    Case("box-missing", rules("box-receipt"), lambda lane: lane.box.unlink()),
    Case("receipt-missing", rules("box-receipt"), lambda lane: lane.receipt.unlink()),
    Case("receipt-tampered", rules("box-receipt"), tampered_receipt),
    Case("profile-changed", rules("box-receipt"), changed_profile),
    Case("candidate-unknown-commit", rules("candidate-missing"), declared(commit="1" * 40)),
    Case("candidate-not-a-repository", rules("candidate-missing"), not_a_repository("candidate")),
    Case("candidate-tag", rules("candidate-missing"), tagged_commit, "is not a commit object"),
    Case("candidate-subdirectory", rules("candidate-missing"),
         lambda lane: setattr(lane, "candidate", lane.candidate / "src"), "is not the top level of a git clone"),
    Case("input-missing", rules("candidate-path"), declared(inputs=["missing.txt"]),
         "input 'missing.txt': 'missing.txt' is not in the candidate commit"),
    Case("input-nested-missing", rules("candidate-path"), declared(inputs=["src/missing.js"]),
         "input 'src/missing.js': 'src/missing.js' is not in the candidate commit"),
    Case("input-through-symlink", rules("candidate-path"), declared(inputs=["linked/index.js"]),
         "input 'linked/index.js': 'linked' is a symbolic link"),
    Case("input-symlink", rules("candidate-path"), declared(inputs=["linked"]),
         "input 'linked': 'linked' is a symbolic link"),
    Case("input-submodule", rules("candidate-path"), declared(inputs=["vendor/sub"]),
         "input 'vendor/sub': 'vendor/sub' is a submodule"),
    Case("input-through-submodule", rules("candidate-path"), declared(inputs=["vendor/sub/x.js"]),
         "input 'vendor/sub/x.js': 'vendor/sub' is a submodule"),
    Case("input-under-file", rules("candidate-path"), declared(inputs=["package.json/x"]),
         "input 'package.json/x': 'package.json' is not a directory"),
    Case("cwd-file", rules("candidate-path"), declared(workingDirectory="README.md"),
         "workingDirectory 'README.md': 'README.md' is not a directory"),
    Case("cwd-missing", rules("candidate-path"), declared(workingDirectory="lib"),
         "workingDirectory 'lib': 'lib' is not in the candidate commit"),
    Case("cwd-nested-file", rules("candidate-path"), declared(workingDirectory="src/index.js"),
         "workingDirectory 'src/index.js': 'src/index.js' is not a directory"),
    Case("secret-unset", rules("secret-missing"), declared(secrets=["LANE_UNSET_SECRET"])),
    Case("secret-empty", rules("secret-missing"),
         lambda lane: (lane.monkeypatch.setenv("LANE_EMPTY_SECRET", ""), lane.declare(secrets=["LANE_EMPTY_SECRET"]))),
    Case("engine-dirty", rules("engine-commit"), dirty_engine),
    Case("engine-not-a-repository", rules("engine-commit"), not_a_repository("engine")),
    Case("record-broken", rules("earlier-record-invalid"), broken_record),
    Case("record-unhashed", rules("earlier-record-invalid"), unhashed_record),
    Case("uuid-reused", rules("reused-instance-uuid"), reused),
    Case("name-registered", rules("instance-name-in-use"),
         fixed_name(lambda lane: lane.set_host(vms={teardown_tests.OTHER_UUID: FIXED_NAME}))),
    Case("name-run-directory", rules("instance-name-in-use"), fixed_name(lambda lane: (lane.work / FIXED_NAME).mkdir())),
    Case("name-evidence", rules("instance-name-in-use"),
         fixed_name(lambda lane: (lane.evidence / FIXED_NAME).symlink_to("/nonexistent"))),
    Case("name-record-file", rules("instance-name-in-use", "earlier-record-invalid"),
         fixed_name(lambda lane: (lane.records / f"{FIXED_NAME}{plan.RECORD_SUFFIX}").touch())),
    Case("name-recorded", rules("instance-name-in-use"), recorded_name),
    Case("virtualbox-version", rules("host-virtualbox"), host(modes=("version-fail",))),
    Case("virtualbox-version-form", rules("host-virtualbox"), host(version="7.2.6 extra")),
    Case("virtualbox-list", rules("host-virtualbox"), host(modes=("list-fail",))),
    Case("virtualbox-properties", rules("host-virtualbox"), host(modes=("properties-fail",))),
    Case("virtualbox-no-folder", rules("host-virtualbox"), host(folder=None)),
    Case("virtualbox-relative-folder", rules("host-virtualbox"), host(folder="VirtualBox VMs")),
    Case("vagrant-version", rules("host-vagrant"), host(modes=("vagrant-fail",))),
    Case("vagrant-version-form", rules("host-vagrant"), host(vagrant="2.4.9")),
    Case("disk-space", rules("host-disk-space"), little_disk),
    Case("port-taken", rules("ssh-port"), taken_port),
]


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_each_failed_check_is_a_named_refusal(lane, capsys, case):
    case.arrange(lane)
    found = refused_rules(lane, capsys)
    assert set(found) == case.rules, found
    if case.detail:
        assert any(case.detail in detail for details in found.values() for detail in details), found
    value = json.loads(lane.plan_out.read_text(encoding="utf-8"))
    assert all(call in READ_ONLY_CALLS for call in lane.calls())
    if case.rules & {"declaration-json", "declaration-fields", "box-digest", "box-receipt"} or \
            case.rules & set(lane_declaration.DECLARATION_RULES):
        assert value["steps"] == []


def test_every_rule_has_a_negative_control():
    assert set().union(*(case.rules for case in CASES)) == set(plan.RULES)
    assert len(plan.RULES) == len(set(plan.RULES))


def test_independent_refusals_are_all_reported(lane, capsys, monkeypatch):
    lane.declare(commit="main", timeoutSeconds=0, outputs=["disk.vdi"])
    appended_box(lane)
    dirty_engine(lane)
    lane.set_host(modes=("vagrant-fail",))
    assert set(refused_rules(lane, capsys)) == {"commit", "timeout", "output-forbidden", "box-digest",
                                                "engine-commit", "host-vagrant"}


def test_tampered_box_is_refused_before_its_receipt_is_checked(lane, capsys, monkeypatch):
    """TEST-103: the box digest check stands alone, so it holds even if the receipt check were skipped."""
    appended_box(lane)
    monkeypatch.setattr(plan, "verify_box_receipt", lambda *args, **kwargs: {})
    found = refused_rules(lane, capsys)
    assert set(found) == {"box-digest"}
    assert "differs from the" in found["box-digest"][0]


def test_a_uuid_an_earlier_record_saw_removed_is_refused_while_registered(lane, capsys):
    """TEST-103 / REQ-101: VirtualBox must not list an instance UUID that a lane record observed removed."""
    reused(lane)
    found = refused_rules(lane, capsys)
    assert found == {"reused-instance-uuid": [f"VirtualBox lists {RECORDED_UUID}, which an earlier lane record "
                                              "observed removed or absent"]}


def test_outputs_named_like_but_not_forbidden_are_allowed(lane, capsys):
    lane.declare(outputs=["known_hosts.txt", "vdi.json", "dist/vm.vbox.md"])
    assert lane.run(capsys)[0] == 0


def test_a_declaration_with_no_inputs_needs_only_the_commit(lane, capsys):
    lane.declare(inputs=[], outputs=[])
    assert lane.run(capsys)[0] == 0


@pytest.fixture
def listings(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """The pathspecs of each ``git ls-tree`` call the plan makes, in order."""
    found: list[list[str]] = []
    original = plan.Host.git

    def recording(self, repository, *arguments):
        if arguments[0] == "ls-tree":
            found.append(list(arguments[arguments.index("--") + 1:]))
        return original(self, repository, *arguments)

    monkeypatch.setattr(plan.Host, "git", recording)
    return found


def test_nested_inputs_and_working_directory_are_found_in_the_commit(lane, capsys, listings):
    lane.declare(inputs=["src/index.js", "src", "package.json"], workingDirectory="src")
    code, value, _, err = lane.run(capsys)
    assert code == 0, err
    assert [step["cwd"] for step in value["steps"] if step["name"] == "workload"] == ["src"]
    assert listings == [["package.json", "src"], ["src/index.js"]]


def test_nothing_is_listed_below_a_symbolic_link_submodule_or_file(lane, capsys, listings):
    lane.declare(inputs=["linked/deep/x.js", "vendor/sub/x.js", "package.json/x"])
    assert set(refused_rules(lane, capsys)) == {"candidate-path"}
    assert listings == [["linked", "package.json", "vendor"], ["vendor/sub"]]


# ---------------------------------------------------------------- usage


@pytest.mark.parametrize("overrides, message", [
    ({"--box": "lane.box"}, "--box must be an absolute path"),
    ({"--work-dir": "/nonexistent/work"}, "directory must be an existing absolute path"),
    ({"--ssh-port": "80"}, "--ssh-port must be from 1024 to 65535"),
    ({"--ssh-port": "65536"}, "--ssh-port must be from 1024 to 65535"),
])
def test_usage_errors_write_no_plan(lane, capsys, overrides, message):
    code, value, _, err = lane.run(capsys, **overrides)
    assert code == 2 and value is None and message in err
    assert lane.calls() == []


def test_a_directory_others_can_read_is_refused(lane, capsys):
    lane.records.chmod(0o750)
    code, value, _, err = lane.run(capsys)
    assert code == 2 and value is None and "mode 700 or stricter" in err


def test_an_existing_plan_is_never_replaced(lane, capsys):
    assert lane.run(capsys)[0] == 0
    before = lane.plan_out.read_bytes()
    code, _, _, err = lane.run(capsys)
    assert code == 2 and "plan output already exists" in err
    assert lane.plan_out.read_bytes() == before


def test_an_existing_plan_digest_is_never_replaced(lane, capsys):
    digest = Path(f"{lane.plan_out}.sha256")
    digest.write_text("kept\n", encoding="utf-8")
    code, value, _, err = lane.run(capsys)
    assert code == 2 and value is None and "plan output already exists" in err
    assert digest.read_text(encoding="utf-8") == "kept\n" and lane.calls() == []


@pytest.mark.parametrize("tool", ["--vboxmanage", "--vagrant", "--git"])
def test_a_missing_tool_exits_127(lane, capsys, tool):
    code, value, _, err = lane.run(capsys, **{tool: str(lane.root / "missing-tool")})
    assert code == 127 and value is None and "missing" in err


def test_a_plan_that_cannot_be_written_fails(lane, capsys, monkeypatch):
    def full(path, hash_path, content):
        raise OSError("no space left on device")
    monkeypatch.setattr(plan, "write_new_with_digest", full)
    code, _, _, err = lane.run(capsys)
    assert code == 2 and "cannot write the plan: no space left on device" in err


# Valid JSON, so the test can read a kept plan back the way it reads a written one.
KEPT = '{"kept": true}\n'


@pytest.mark.parametrize("late", ["plan", "digest"])
def test_an_output_that_appears_while_planning_is_kept(lane, capsys, monkeypatch, late):
    """Two plans racing for one --plan-out: the later writer fails and replaces nothing."""
    digest = Path(f"{lane.plan_out}.sha256")
    target = lane.plan_out if late == "plan" else digest
    make_plan = plan.make_plan

    def racing(*arguments):
        value = make_plan(*arguments)
        target.write_text(KEPT, encoding="utf-8")
        return value

    monkeypatch.setattr(plan, "make_plan", racing)
    code, _, _, err = lane.run(capsys)
    assert code == 2 and "cannot write the plan" in err and "File exists" in err
    assert target.read_text(encoding="utf-8") == KEPT
    if late == "plan":
        assert not digest.exists()
    else:
        with pytest.raises(LaneBoxError):
            common.verify_hash_file(digest, lane.plan_out)
    expected = {lane.plan_out.name, digest.name} if late == "digest" else {lane.plan_out.name}
    assert {path.name for path in lane.plans.iterdir()} == expected


def test_a_candidate_subdirectory_is_refused_even_when_its_paths_match(lane, capsys, tmp_path):
    """Run from a subdirectory, git finds `app/package.json` as `package.json`, which the run would not find."""
    clone = tmp_path / "nested"
    commit = make_repository(clone, {"app/package.json": "{}\n", "app/package-lock.json": "{}\n"})
    lane.declare(commit=commit)
    found = refused_rules(lane, capsys, **{"--candidate-repo": str(clone / "app")})
    assert found == {"candidate-missing": [f"{clone / 'app'} is not the top level of a git clone"]}


def test_written_files_are_new_private_and_leave_no_temporary_file(tmp_path):
    folder = private_dir(tmp_path / "out")
    target, digest = folder / "out.json", folder / "out.json.sha256"
    write_new_with_digest(target, digest, "{}\n")
    assert stat.S_IMODE(target.stat().st_mode) == stat.S_IMODE(digest.stat().st_mode) == 0o600
    assert digest.read_text(encoding="utf-8") == common.hash_line(target)
    with pytest.raises(FileExistsError):
        write_new_with_digest(target, digest, "[]\n")
    assert target.read_text(encoding="utf-8") == "{}\n"
    assert sorted(path.name for path in folder.iterdir()) == ["out.json", "out.json.sha256"]


# ---------------------------------------------------------------- units


def test_rfc_8785_orders_keys_by_utf_16_code_units():
    value = {"\u20ac": "Euro Sign", "\r": "Carriage Return", "\ufb33": "Hebrew Letter Dalet With Dagesh",
             "1": "One", "\U0001f600": "Emoji: Grinning Face", "\u0080": "Control",
             "\u00f6": "Latin Small Letter O With Diaeresis"}
    text = lane_declaration.canonical(value)
    assert [key for key in json.loads(text)] == ["\r", "1", "\u0080", "\u00f6", "\u20ac", "\U0001f600", "\ufb33"]


def test_rfc_8785_escapes_only_what_json_requires():
    value = {"b": [True, False, -0, 9007199254740991], "a": "\u0000\b\t\n\f\r\u001f\"\\/\u007f\u2028\u00e9"}
    assert lane_declaration.canonical(value) == (
        '{"a":"\\u0000\\b\\t\\n\\f\\r\\u001f\\"\\\\/\u007f\u2028\u00e9","b":[true,false,0,9007199254740991]}')


@pytest.mark.parametrize("value", [None, 1.5, -(2 ** 53), {"a": {1, 2}}, "\ud800", [object()]])
def test_values_without_a_canonical_form_are_refused(value):
    with pytest.raises(lane_declaration.NotCanonical):
        lane_declaration.canonical(value)


def test_the_declaration_digest_ignores_formatting(lane, tmp_path):
    compact = tmp_path / "compact.json"
    compact.write_text(json.dumps(lane.data(), separators=(",", ":"), sort_keys=True), encoding="utf-8")
    spaced = tmp_path / "spaced.json"
    spaced.write_text(json.dumps(dict(reversed(lane.data().items())), indent=4), encoding="utf-8")
    first, second = lane_declaration.load_declaration(compact), lane_declaration.load_declaration(spaced)
    assert first[1] == second[1] == lane_declaration.canonical_sha256(lane.data())
    assert first[0] == second[0] and first[2] == second[2] == []


def test_the_example_declaration_is_valid_and_engine_neutral():
    found, digest, refusals = lane_declaration.load_declaration(EXAMPLE_DECLARATION)
    assert refusals == [] and found is not None and len(digest) == 64
    assert plan.contract_of(found.toolchain) == plan.contract_of(box_tests.example()["toolchain"])
    text = EXAMPLE_DECLARATION.read_text(encoding="utf-8").lower()
    assert not any(word in text for word in ("labview", "vipm", "benchmark", "vitechnologies", "svelderrainruiz"))


def test_check_disk_adds_needs_that_share_a_file_system(tmp_path):
    (tmp_path / "a").mkdir()
    disks, refusals = plan.check_disk([(tmp_path / "a" / "missing" / "deeper", 5), (tmp_path, 7)])
    assert refusals == [] and len(disks) == 1
    assert disks[0]["requiredBytes"] == 12 and disks[0]["path"] == str(tmp_path / "a")


def test_render_vagrantfile_refuses_values_it_cannot_quote():
    hardware = box_tests.example()["virtualBox"]
    for name, port, values in ((f'{FIXED_NAME}"', 2222, hardware), (FIXED_NAME, 22, hardware),
                               (FIXED_NAME, 2222, dict(hardware, cpus=True)), (FIXED_NAME, 2222, dict(hardware, vramMb="8"))):
        with pytest.raises(LaneBoxError):
            plan.render_vagrantfile(name, port, values)


def test_host_queries_fail_closed(tmp_path, monkeypatch):
    missing = plan.Host(str(tmp_path / "VBoxManage"), str(tmp_path / "vagrant"), str(tmp_path / "git"))
    assert missing.virtualbox_version() is None and missing.vagrant_version() is None
    assert missing.registered() is None and missing.machine_folder() is None
    assert missing.git(tmp_path, "status") is None
    slow = box_tests.Host._stub(private_dir(tmp_path / "bin") / "VBoxManage", "\nimport time\ntime.sleep(5)\n")
    monkeypatch.setattr(plan, "QUERY_TIMEOUT", 0.2)
    assert plan.Host(str(slow), str(slow), "git").virtualbox_version() is None


class FakeHost(plan.Host):
    def __init__(self, results: dict[str, subprocess.CompletedProcess | None]) -> None:
        super().__init__("VBoxManage", "vagrant", "git")
        self.results = results

    def git(self, repository, *arguments):
        return self.results.get(arguments[0])


def completed(code: int, stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], code, stdout, "")


def test_git_failures_are_refusals(tmp_path):
    found, _, _ = lane_declaration.load_declaration(EXAMPLE_DECLARATION)
    unlisted = FakeHost({"rev-parse": completed(0, "\n"), "cat-file": completed(0, "commit\n"),
                         "ls-tree": completed(128)})
    assert plan.check_candidate(unlisted, tmp_path, found) == [
        Refusal("candidate-path", f"cannot list the tree of commit {found.commit}")]
    top = completed(0, "\n")
    for failing in ({"rev-parse": None}, {"rev-parse": top, "cat-file": None},
                    {"rev-parse": completed(128), "cat-file": completed(0, "commit\n"), "ls-tree": completed(0)},
                    {"rev-parse": top, "cat-file": completed(128, "commit\n"), "ls-tree": completed(0)}):
        assert [refusal.rule for refusal in plan.check_candidate(FakeHost(failing), tmp_path, found)] == [
            "candidate-missing"]
    unreadable = FakeHost({"rev-parse": completed(0, "a" * 40 + "\n"), "status": None})
    assert plan.engine_commit(unreadable, tmp_path)[1][0].rule == "engine-commit"


def test_registered_parses_only_well_formed_lines(tmp_path, monkeypatch):
    stub = box_tests.Host._stub(private_dir(tmp_path / "bin") / "VBoxManage", """
print('"plain" {0A0A0A0A-1111-4222-8333-444444444444}')
print('"has {braces}" {0b0b0b0b-5555-4666-8777-888888888888}')
print('"bad uuid" {not-a-uuid}')
print('unquoted {0c0c0c0c-5555-4666-8777-888888888888}')
print('" {0d0d0d0d-5555-4666-8777-888888888888}')
""")
    assert plan.Host(str(stub), "vagrant", "git").registered() == {
        "0a0a0a0a-1111-4222-8333-444444444444": "plain", "0b0b0b0b-5555-4666-8777-888888888888": "has {braces}"}


# ---------------------------------------------------------------- TEST-105: no runner registration


class Recorder(http.server.BaseHTTPRequestHandler):
    requests: list[str] = []

    def _record(self) -> None:
        Recorder.requests.append(f"{self.command} {self.path}")
        self.send_response(403)
        self.end_headers()

    do_GET = do_POST = do_PUT = do_DELETE = do_CONNECT = _record

    def log_message(self, *args) -> None:
        pass


NETWORK_STUB = """
import json, os, sys
with open(os.environ["LANE_NETWORK_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps([os.path.basename(sys.argv[0]), *sys.argv[1:]]) + "\\n")
sys.exit(1)
"""


@pytest.fixture
def detector(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Route every way the lane could reach GitHub to a recorder, and hand back what it saw."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
    Recorder.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    proxy = f"http://127.0.0.1:{server.server_address[1]}"
    stubs = private_dir(tmp_path / "network-bin")
    for tool in ("gh", "curl", "wget"):
        box_tests.Host._stub(stubs / tool, NETWORK_STUB)
    log = tmp_path / "network.log"
    monkeypatch.setenv("LANE_NETWORK_LOG", str(log))
    monkeypatch.setenv("PATH", f"{stubs}{os.pathsep}{os.environ['PATH']}")
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, proxy)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setenv("GITHUB_API_URL", proxy)
    monkeypatch.setenv("GH_TOKEN", CANARY)
    monkeypatch.setenv("GITHUB_TOKEN", CANARY)

    def seen() -> list[str]:
        commands = [" ".join(json.loads(line)) for line in log.read_text(encoding="utf-8").splitlines()] \
            if log.exists() else []
        return commands + list(Recorder.requests)

    yield seen
    server.shutdown()
    server.server_close()


def test_lane_commands_make_no_runner_registration_requests(lane, detector, tmp_path, monkeypatch):
    """TEST-105: plan, teardown, and verify-teardown run with no GitHub request, run as real processes."""
    entry = [sys.executable, str(SCRIPTS / "Invoke-Lane.py")]
    environment = dict(os.environ)
    subprocess.run([*entry, *lane.args()], env=environment, capture_output=True, timeout=120, check=False)
    assert lane.plan_out.exists()
    down = teardown_tests.Host(tmp_path / "down", monkeypatch)
    environment = dict(os.environ)
    for argv in (down.args(), down.args(run=True), ["verify-teardown", str(down.receipt)]):
        result = subprocess.run([*entry, *argv], env=environment, capture_output=True, text=True, timeout=120,
                                check=False)
        assert result.returncode == 0, result.stderr
        assert CANARY not in result.stdout + result.stderr
    assert detector() == []


def test_the_detector_catches_a_lane_that_requests_runner_tokens(lane, detector, capsys, monkeypatch):
    """TEST-105 control: a mutant lane that asks GitHub for registration or removal tokens is caught."""
    original = plan.check_secrets

    def mutant(declaration, environ):
        subprocess.run(["gh", "api", "-X", "POST", "repos/o/r/actions/runners/registration-token"], check=False)
        try:
            urllib.request.urlopen(f"{environ['GITHUB_API_URL']}/repos/o/r/actions/runners/remove-token", timeout=10)
        except OSError:
            pass
        return original(declaration, environ)

    monkeypatch.setattr(plan, "check_secrets", mutant)
    lane.run(capsys)
    seen = " ".join(detector())
    assert "actions/runners/registration-token" in seen and "actions/runners/remove-token" in seen


def test_no_lane_source_names_a_runner_token_endpoint():
    for path in sorted((SCRIPTS / "lane").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        ast.parse(text)
        for endpoint in ("registration-token", "remove-token", "actions/runners", "api.github.com"):
            assert endpoint not in text, (path.name, endpoint)
