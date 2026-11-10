"""Appliance box packager (package-vagrant-box.py): its contract before the runner scan.

The packager runs in process with a fake host behind ``subprocess.run`` and ``subprocess.Popen``. Storage,
VBoxManage, Vagrant, and the activation and public-appliance verifiers are fakes; the box receipt verifier is real,
and the fake ``vagrant package`` writes a real box for it to check. Issue: VITechnologies/projectbuddy#195.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import runpy
import shutil
import signal
import subprocess
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
APPLIANCE = REPOSITORY / "vm" / "ubuntu-labview"
PACKAGER = APPLIANCE / "package-vagrant-box.py"
BOX_VERIFIER = APPLIANCE / "verify-vagrant-box-receipt.py"
MANIFEST = APPLIANCE / "provisioning-manifest.json"
EMBEDDED = APPLIANCE / "Vagrantfile.box"
REAL_POPEN = subprocess.Popen
VBOX = "/host/bin/VBoxManage"
VAGRANT = "/host/bin/vagrant"
NAME = "appliance"
VM_UUID = "16fd2706-8baf-433b-82eb-8c7fada847da"
ACTIVATED_UUID = "2b1c3d4e-5f60-4718-9a2b-3c4d5e6f7081"
PACKAGE_UUID = "3c2d4e5f-6071-4829-8b3c-4d5e6f708192"
TAKEN_UUID = "4d3e5f60-7182-493a-9c4d-5e6f708192a3"
PUBLIC_UUID = "5e4f6071-8293-4a4b-8d5e-6f708192a3b4"
BOOT = "0f8fad5b-d9cb-469f-a165-70867728950e"
ACTIVATED = "labview2026-q3-pro-activated-ready"
PACKAGE_READY = "labview2026-q3-pro-package-ready"
PUBLIC_SNAPSHOT = "labview2026-q3-public-ready"
DEVICE = "/dev/nvme0n1p2"
CAPTURES = {
    "knownAnswerCapture": "activation-known-answer-capture.json",
    "runtimeState": "sandbox-runtime-state.json",
    "vipmAbout": "sandbox-vipm-about.json",
    "vipmLabview": "sandbox-vipm-labview.json",
    "vipmInstalled": "sandbox-vipm-installed.json",
}
WRAPPER = (
    'Vagrant::Config.run do |config|\n'
    '  # The contents below were provided by the Packer Vagrant post-processor\n'
    '  # and the base box settings follow.\n'
    '  # Do not edit.\n'
    '  config.vm.base_mac = "080027ABCDEF"\n'
    'end\n\n'
    '# Load include vagrant file if it exists after the auto-generated\n'
    '# so it can override any of the settings\n'
    'include_vagrantfile = File.expand_path("../include/_Vagrantfile", __FILE__)\n'
    'load include_vagrantfile if File.exist?(include_vagrantfile)\n'
)


def real_run(argv: list[str]) -> tuple[int, str]:
    """Run ``argv`` for real; ``subprocess.run`` would reach the fake ``Popen``."""
    with REAL_POPEN(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
        _, error = process.communicate(timeout=60)
    return process.returncode, error


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def private_file(path: Path, content: object) -> Path:
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content if isinstance(content, str) else json.dumps(content, indent=2), encoding="utf-8")
    path.chmod(0o600)
    return path


def hash_file(target: Path) -> Path:
    return private_file(Path(f"{target}.sha256"), f"{sha256(target)}  {target.name}\n")


def vm_info(**changes: str) -> dict[str, str]:
    info = {"name": NAME, "UUID": VM_UUID, "VMState": "poweroff",
            "CurrentSnapshotName": ACTIVATED, "CurrentSnapshotUUID": ACTIVATED_UUID}
    info.update(changes)
    return {key: value for key, value in info.items() if value is not None}


def listing(values: dict[str, str]) -> str:
    return "".join(f'{key}="{value}"\n' for key, value in values.items())


def write_box(path: Path, wrapper: str = WRAPPER) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, data in (("box.ovf", b"<Envelope/>"), ("Vagrantfile", wrapper.encode()),
                           ("include/_Vagrantfile", EMBEDDED.read_bytes())):
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))


class Process:
    """``subprocess.Popen``: ``hangs`` waits time out before the exit code is returned."""

    def __init__(self, argv: list[str], code: int, hangs: int) -> None:
        self.args, self.code, self.hangs, self.pid = argv, code, hangs, 4242

    def wait(self, timeout=None):
        if self.hangs:
            self.hangs -= 1
            raise subprocess.TimeoutExpired(self.args, timeout)
        return self.code


class Host:
    """The packager's host: storage, the sibling verifiers, VBoxManage, and Vagrant."""

    def __init__(self) -> None:
        self.mount = f"{DEVICE} ext4 rw,relatime\n"
        self.devices: dict[str, str] = {}
        self.findmnt_code = 0
        self.transport = (0, "nvme\n")
        self.verifiers = {"verify-activation-receipt.py": 0, "verify-public-appliance-receipt.py": 0}
        self.box_verifier: int | None = None
        self.info = vm_info()
        self.info_code = 0
        self.snapshots = {"SnapshotName": ACTIVATED, "SnapshotUUID": ACTIVATED_UUID, "SnapshotDescription": ""}
        self.snapshots_code = 0
        self.take = (0, vm_info(CurrentSnapshotName=PACKAGE_READY, CurrentSnapshotUUID=TAKEN_UUID))
        self.package = 0
        self.wrapper = WRAPPER
        self.box_bytes: bytes | None = None
        self.hangs = 0
        self.calls: list[list[str]] = []
        self.environments: dict[str, dict[str, str]] = {}
        self.killed: list[int] = []

    def run(self, argv, **_options) -> subprocess.CompletedProcess:
        self.calls.append(list(argv))
        program = Path(argv[0]).name
        if program == "findmnt":
            if argv[3] == "SOURCE,FSTYPE,OPTIONS":
                return subprocess.CompletedProcess(argv, self.findmnt_code, self.mount.replace(
                    DEVICE, self.devices.get(argv[-1], DEVICE)), "")
            return subprocess.CompletedProcess(argv, self.findmnt_code, f"{self.devices.get(argv[-1], DEVICE)}\n", "")
        if program == "lsblk":
            return subprocess.CompletedProcess(argv, self.transport[0], self.transport[1], "")
        if argv[0] == VBOX and argv[1] == "showvminfo":
            return subprocess.CompletedProcess(argv, self.info_code, listing(self.info), "")
        if argv[0] == VBOX and argv[1:4] == ["snapshot", VM_UUID, "list"]:
            return subprocess.CompletedProcess(argv, self.snapshots_code, listing(self.snapshots), "")
        raise AssertionError(f"unexpected run: {argv}")

    def popen(self, argv, env=None, **_options) -> Process:
        self.calls.append(list(argv))
        program = Path(argv[0]).name
        if env is not None:
            self.environments[program] = env
        if program in self.verifiers:
            return Process(argv, self.verifiers[program], 0)
        if program == BOX_VERIFIER.name:
            if self.box_verifier is not None:
                return Process(argv, self.box_verifier, 0)
            code, error = real_run([sys.executable, *argv])
            sys.stderr.write(error)
            return Process(argv, code, 0)
        if argv[0] == VBOX and argv[1:4] == ["snapshot", VM_UUID, "take"]:
            code, refreshed = self.take
            if code == 0:
                self.info = refreshed
            return Process(argv, code, 0)
        if argv[0] == VAGRANT and argv[1] == "package":
            output = Path(argv[argv.index("--output") + 1])
            if self.package == 0:
                if self.box_bytes is None:
                    write_box(output, self.wrapper)
                else:
                    output.write_bytes(self.box_bytes)
            return Process(argv, self.package, self.hangs)
        raise AssertionError(f"unexpected Popen: {argv}")

    def programs(self) -> list[str]:
        return [Path(argv[0]).name for argv in self.calls]

    def mutations(self) -> list[list[str]]:
        return [argv for argv in self.calls
                if argv[0] == VAGRANT or (argv[0] == VBOX and "take" in argv)]


@pytest.fixture
def packaging(tmp_path, monkeypatch):
    previous = os.umask(0o077)
    os.umask(previous)
    evidence = private_dir(tmp_path / "evidence")
    base = private_file(evidence / "base-bootstrap-receipt.json", {"schema": "base"})
    public = private_file(evidence / "public-provisioning-receipt.json", {"schema": "public"})
    public_appliance = private_file(evidence / "public-appliance-receipt.json",
                                    {"snapshot": {"name": PUBLIC_SNAPSHOT, "uuid": PUBLIC_UUID}})
    hash_file(public_appliance)
    captures = {key: private_file(evidence / filename, {"capture": key}) for key, filename in CAPTURES.items()}
    activation = private_file(evidence / "activation-receipt.json", {
        "vm": {"bootId": BOOT}, "evidenceSha256": {key: sha256(path) for key, path in captures.items()}})
    hash_file(activation)
    package_dir = private_dir(tmp_path / "package")
    artifact = private_file(package_dir / "vi_technologies_lib_super_network_streams-2.0.0.21.vip", b"vip package")
    report = private_file(package_dir / "package-build-report.json", {
        "schema": "projectbuddy/linux-vip-package-proof@1", "outcome": "PASS",
        "vm": {"name": NAME, "uuid": VM_UUID, "baseSnapshotName": ACTIVATED, "baseSnapshotUuid": ACTIVATED_UUID},
        "package": {"name": "vi_technologies_lib_super_network_streams", "version": "2.0.0.21",
                    "status": "verified-installed", "sha256": sha256(artifact), "file": artifact.name},
    })
    vagrant_home = private_dir(tmp_path / "vagrant-home")
    private_dir(vagrant_home / "boxes")
    out = private_dir(tmp_path / "out")
    state = SimpleNamespace(
        host=Host(), evidence=evidence, base=base, public=public, public_appliance=public_appliance,
        activation=activation, captures=captures, report=report, artifact=artifact, vagrant_home=vagrant_home,
        temp=private_dir(tmp_path / "tmp"), out=out, box=out / "appliance.box",
        receipt=out / "appliance-box-receipt.json", receipt_hash=out / "appliance-box-receipt.json.sha256")
    state.options = {
        "--vm-name": NAME, "--vm-uuid": VM_UUID, "--activated-snapshot-uuid": ACTIVATED_UUID,
        "--evidence-dir": str(evidence), "--package-report": str(report), "--package-artifact": str(artifact),
        "--vagrant-home": str(vagrant_home), "--temp-dir": str(state.temp), "--output": str(state.box),
        "--receipt-out": str(state.receipt), "--vboxmanage": VBOX, "--vagrant": VAGRANT,
    }
    monkeypatch.setattr(subprocess, "run", lambda argv, **options: state.host.run(argv, **options))
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **options: state.host.popen(argv, **options))
    monkeypatch.setattr(os, "killpg", lambda pid, sent: state.host.killed.append(sent))

    def run(*extra: str, drop: tuple[str, ...] = (), **changes: str) -> int:
        options = {key: value for key, value in state.options.items() if key not in drop}
        options.update({f"--{key.replace('_', '-')}": value for key, value in changes.items()})
        argv = [*(word for pair in options.items() for word in pair), *extra]
        monkeypatch.setattr(sys, "argv", [str(PACKAGER), *argv])
        return runpy.run_path(str(PACKAGER), run_name="packager")["main"]()

    state.run = run
    yield state
    os.umask(previous)


def at_package_ready(state, description: str | None = None) -> None:
    marker = (f"projectbuddy/package-ready@1;activated={ACTIVATED_UUID};"
              f"report={sha256(state.report)};artifact={sha256(state.artifact)}")
    state.host.info = vm_info(CurrentSnapshotName=PACKAGE_READY, CurrentSnapshotUUID=PACKAGE_UUID)
    state.host.snapshots.update({"SnapshotName-1": PACKAGE_READY, "SnapshotUUID-1": PACKAGE_UUID,
                                 "SnapshotDescription-1": marker if description is None else description})
    state.options["--package-snapshot-uuid"] = PACKAGE_UUID


def rewrite(path: Path, change) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    change(data)
    private_file(path, data)


def outputs(state) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in (state.box, state.receipt, state.receipt_hash) if path.exists()}


def verifier_calls(state) -> list[list[str]]:
    return [argv for argv in state.host.calls if Path(argv[0]).name.startswith("verify-")]


# Planning.

def test_the_packager_plans_from_the_activated_snapshot(packaging, capsys):
    assert packaging.run() == 0
    assert capsys.readouterr().out.splitlines() == [
        "Vagrant box packaging: PLAN",
        f"command={VAGRANT} package --base {VM_UUID} --output {packaging.box} --vagrantfile {EMBEDDED}",
        f"source_vm_uuid={VM_UUID}",
        f"activated_snapshot_uuid={ACTIVATED_UUID}",
        "package_snapshot=create",
    ]
    assert verifier_calls(packaging) == [
        [str(APPLIANCE / "verify-activation-receipt.py"), str(packaging.activation), "--vm-name", NAME,
         "--vm-uuid", VM_UUID, "--boot-id", BOOT, "--snapshot-uuid", ACTIVATED_UUID],
        [str(APPLIANCE / "verify-public-appliance-receipt.py"), str(packaging.public_appliance),
         "--base-receipt", str(packaging.base), "--public-receipt", str(packaging.public),
         "--manifest", str(MANIFEST), "--vm-name", NAME, "--vm-uuid", VM_UUID,
         "--snapshot-name", PUBLIC_SNAPSHOT, "--snapshot-uuid", PUBLIC_UUID],
    ]
    assert packaging.host.calls[-1] == [VBOX, "showvminfo", NAME, "--machinereadable"]
    assert packaging.host.mutations() == []
    assert outputs(packaging) == {}


def test_the_packager_plans_from_a_marked_package_snapshot(packaging, capsys):
    at_package_ready(packaging)
    assert packaging.run() == 0
    output = capsys.readouterr().out.splitlines()
    assert output[0] == "Vagrant box packaging: PLAN" and output[-1] == f"package_snapshot={PACKAGE_UUID}"
    assert packaging.host.calls[-1] == [VBOX, "snapshot", VM_UUID, "list", "--machinereadable"]
    assert packaging.host.mutations() == []


def test_the_packager_checks_every_storage_location(packaging, capsys):
    assert packaging.run() == 0
    resolved = {argv[-1] for argv in packaging.host.calls if argv[0] == "findmnt"}
    assert resolved == {str(path) for path in (
        packaging.evidence, packaging.vagrant_home, packaging.vagrant_home / "boxes", packaging.temp,
        packaging.out, packaging.report.parent)}
    assert {argv[-1] for argv in packaging.host.calls if argv[0] == "lsblk"} == {DEVICE}


def test_the_packager_accepts_uppercase_uuids(packaging, capsys):
    assert packaging.run(vm_uuid=VM_UUID.upper(), activated_snapshot_uuid=ACTIVATED_UUID.upper()) == 0
    assert f"source_vm_uuid={VM_UUID}" in capsys.readouterr().out


# Packaging.

def test_the_packager_takes_the_package_snapshot_and_packages(packaging, capsys):
    assert packaging.run("--run") == 0
    assert capsys.readouterr().out.splitlines() == [
        f"Vagrant box packaging: PASS: {packaging.box}", f"Vagrant box packaging: PASS: {packaging.receipt}"]
    marker = (f"projectbuddy/package-ready@1;activated={ACTIVATED_UUID};"
              f"report={sha256(packaging.report)};artifact={sha256(packaging.artifact)}")
    assert packaging.host.mutations() == [
        [VBOX, "snapshot", VM_UUID, "take", PACKAGE_READY, "--description", marker],
        [VAGRANT, "package", "--base", VM_UUID, "--output", str(packaging.box), "--vagrantfile", str(EMBEDDED)],
    ]
    environment = packaging.host.environments["vagrant"]
    assert (environment["VAGRANT_HOME"], environment["TMPDIR"]) == (str(packaging.vagrant_home), str(packaging.temp))
    receipt = json.loads(packaging.receipt.read_text(encoding="utf-8"))
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert receipt == {
        "schema": "projectbuddy/ubuntu-vagrant-box@1",
        "source": {"vm": {"name": NAME, "uuid": VM_UUID},
                   "activatedSnapshot": {"name": ACTIVATED, "uuid": ACTIVATED_UUID},
                   "snapshot": {"name": PACKAGE_READY, "uuid": TAKEN_UUID}},
        "box": {"filename": packaging.box.name, "sha256": sha256(packaging.box),
                "sizeBytes": packaging.box.stat().st_size},
        "provenanceSha256": {
            "baseReceiptSha256": sha256(packaging.base),
            "publicProvisioningReceiptSha256": sha256(packaging.public),
            "publicApplianceReceiptSha256": sha256(packaging.public_appliance),
            "activationReceiptSha256": sha256(packaging.activation),
            "packageBuildReportSha256": sha256(packaging.report),
            "packageArtifactSha256": sha256(packaging.artifact),
            "manifestSha256": sha256(MANIFEST),
            "embeddedVagrantfileSha256": sha256(EMBEDDED),
        },
        "supplyChain": {
            "ubuntuIsoSha256": manifest["os"]["iso"]["sha256"],
            "labviewRepository": manifest["labview"]["repository"],
            "labviewPackage": manifest["labview"]["package"],
            "labviewPackageVersion": manifest["labview"]["packageVersion"],
            "vipmVersion": manifest["vipm"]["version"],
            "vipmArtifactSha256": manifest["vipm"]["sha256"],
        },
        "failures": [],
        "outcome": "PASS",
    }
    assert packaging.receipt_hash.read_text(encoding="utf-8") == (
        f"{sha256(packaging.receipt)}  {packaging.receipt.name}\n")
    for path in (packaging.box, packaging.receipt, packaging.receipt_hash):
        assert path.stat().st_mode & 0o777 == 0o600


def test_the_packager_packages_an_existing_package_snapshot(packaging, capsys):
    at_package_ready(packaging)
    assert packaging.run("--run") == 0
    assert [argv[1] for argv in packaging.host.mutations()] == ["package"]
    receipt = json.loads(packaging.receipt.read_text(encoding="utf-8"))
    assert receipt["source"]["snapshot"] == {"name": PACKAGE_READY, "uuid": PACKAGE_UUID}


def test_the_box_verifier_checks_what_the_packager_wrote(packaging, capsys):
    assert packaging.run("--run") == 0
    verify = verifier_calls(packaging)[-1]
    assert verify == [
        str(BOX_VERIFIER), str(packaging.receipt), "--box", str(packaging.box), "--manifest", str(MANIFEST),
        "--base-receipt", str(packaging.base), "--public-receipt", str(packaging.public),
        "--public-appliance-receipt", str(packaging.public_appliance),
        "--activation-receipt", str(packaging.activation), "--package-report", str(packaging.report),
        "--package-artifact", str(packaging.artifact), "--embedded-vagrantfile", str(EMBEDDED),
        "--vm-name", NAME, "--vm-uuid", VM_UUID, "--activated-snapshot-uuid", ACTIVATED_UUID,
        "--package-snapshot-uuid", TAKEN_UUID, "--hash-out", str(packaging.receipt_hash),
    ]
    rewrite(packaging.receipt, lambda receipt: receipt.update(schema="projectbuddy/ubuntu-vagrant-box@2"))
    code, error = real_run([sys.executable, *verify[:-2]])
    assert code == 2 and "Vagrant box receipt: FAIL: unexpected schema" in error


# Refusals: nothing is created, snapshotted, or packaged.

def break_storage(attribute: str, value) -> object:
    return lambda state: setattr(state.host, attribute, value)


def on_device(name: str) -> object:
    def change(state) -> None:
        state.host.devices[str(getattr(state, name) if name != "boxes" else state.vagrant_home / "boxes")] = (
            "/dev/nvme1n1p1")
    return change


def capture_changed(state) -> None:
    private_file(state.captures["vipmAbout"], {"capture": "changed"})


def report_changed(change) -> object:
    return lambda state: rewrite(state.report, change)


def activation_changed(change) -> object:
    def apply(state) -> None:
        rewrite(state.activation, change)
        state.activation.with_name("activation-receipt.json.sha256").unlink()
        hash_file(state.activation)
    return apply


def exists(name: str) -> object:
    return lambda state: private_file(getattr(state, name), "existing")


def set_info(**changes: str) -> object:
    return lambda state: setattr(state.host, "info", vm_info(**changes))


REFUSALS = [
    (None, dict(vm_name="bad name"), "VM name is invalid"),
    (None, dict(vm_uuid="appliance"), "badly formed hexadecimal UUID string"),
    (None, dict(package_snapshot_uuid="snapshot"), "badly formed hexadecimal UUID string"),
    (lambda s: s.evidence.chmod(0o750), {}, "directory must have mode 700 or stricter"),
    (lambda s: (s.vagrant_home / "boxes").chmod(0o755), {}, "directory must have mode 700 or stricter"),
    (None, dict(evidence_dir="evidence"), "directory must be an existing absolute path"),
    (None, dict(output="appliance.box"), "output paths must be absolute"),
    (exists("box"), {}, "box, receipt, or receipt hash output already exists"),
    (exists("receipt"), {}, "box, receipt, or receipt hash output already exists"),
    (exists("receipt_hash"), {}, "box, receipt, or receipt hash output already exists"),
    (break_storage("mount", f"{DEVICE} fuse.sshfs rw\n"), {}, "private storage cannot use FUSE or allow_other"),
    (break_storage("mount", f"{DEVICE} ext4 rw,allow_other\n"), {}, "private storage cannot use FUSE or allow_other"),
    (break_storage("mount", f"{DEVICE} ext4 rw\n{DEVICE} ext4 rw\n"), {}, "cannot resolve private storage"),
    (break_storage("mount", "tmpfs tmpfs rw\n"), {}, "cannot resolve private storage"),
    (break_storage("findmnt_code", 1), {}, "cannot resolve private storage"),
    (break_storage("transport", (0, "sata\n")), {}, "storage is not internal NVMe"),
    (break_storage("transport", (1, "nvme\n")), {}, "storage is not internal NVMe"),
    (on_device("boxes"), {}, "Vagrant box store and box output must use the same NVMe filesystem"),
    (on_device("temp"), {}, "temporary extraction and box output must use the same NVMe filesystem"),
    (lambda s: s.artifact.chmod(0o644), {}, "package evidence must exist with mode 600 or stricter"),
    (lambda s: s.report.unlink(), {}, "package evidence must exist with mode 600 or stricter"),
    (lambda s: s.base.unlink(), {}, "required input is missing: base-bootstrap-receipt.json"),
    (lambda s: s.base.chmod(0o644), {}, "evidence inputs must have mode 600 or stricter"),
    (lambda s: private_file(s.evidence / "activation-receipt.json.sha256", f"{'0' * 64}  activation-receipt.json\n"),
     {}, "hash mismatch: activation-receipt.json"),
    (lambda s: private_file(s.evidence / "public-appliance-receipt.json.sha256", f"{'0' * 64}  other.json\n"),
     {}, "invalid hash file: public-appliance-receipt.json.sha256"),
    (activation_changed(lambda a: a["vm"].update(bootId="boot")), {}, "badly formed hexadecimal UUID string"),
    (capture_changed, {}, "activation capture hash mismatch: sandbox-vipm-about.json"),
    (report_changed(lambda r: r.update(schema="projectbuddy/linux-vip-package-proof@2")), {},
     "package-build report is not a passing governed receipt"),
    (report_changed(lambda r: r.update(outcome="FAIL")), {}, "package-build report is not a passing governed receipt"),
    (report_changed(lambda r: r["vm"].update(baseSnapshotUuid=PACKAGE_UUID)), {},
     "package-build report VM or base snapshot identity mismatch"),
    (report_changed(lambda r: r["package"].update(sha256="0" * 64)), {},
     "package artifact does not match package-build report"),
    (report_changed(lambda r: r["package"].update(file="other.vip")), {},
     "package artifact does not match package-build report"),
    (report_changed(lambda r: r["package"].update(version="2.0.0.22")), {},
     "package-build report does not match the appliance contract"),
    (report_changed(lambda r: r["package"].update(status="downloaded")), {},
     "package-build report does not match the appliance contract"),
    (lambda s: rewrite(s.public_appliance, lambda p: p["snapshot"].update(uuid="public")) or
     (s.evidence / "public-appliance-receipt.json.sha256").unlink() or hash_file(s.public_appliance), {},
     "badly formed hexadecimal UUID string"),
    (lambda s: s.host.verifiers.update({"verify-activation-receipt.py": 1}), {},
     "activation receipt verification failed"),
    (lambda s: s.host.verifiers.update({"verify-public-appliance-receipt.py": 2}), {},
     "public appliance receipt verification failed"),
    (lambda s: setattr(s.host, "info_code", 1), {}, "cannot read VM identity"),
    (set_info(UUID=PACKAGE_UUID), {}, "registered VM UUID does not match expected identity"),
    (set_info(VMState="running"), {}, "source VM must be powered off"),
    (set_info(VMState="saved"), {}, "source VM must be powered off"),
    (set_info(CurrentSnapshotUUID=PACKAGE_UUID), {},
     "activated-ready snapshot identity does not match packaging inputs"),
    (None, dict(package_snapshot_uuid=PACKAGE_UUID),
     "activated-ready snapshot identity does not match packaging inputs"),
    (set_info(CurrentSnapshotName=PACKAGE_READY, CurrentSnapshotUUID=PACKAGE_UUID), {},
     "package-ready snapshot identity does not match packaging inputs"),
    (set_info(CurrentSnapshotName=PACKAGE_READY, CurrentSnapshotUUID=TAKEN_UUID),
     dict(package_snapshot_uuid=PACKAGE_UUID), "package-ready snapshot identity does not match packaging inputs"),
    (lambda s: at_package_ready(s, "projectbuddy/package-ready@1;activated=other"), {},
     "package-ready snapshot does not carry the expected activated ancestry marker"),
    (lambda s: at_package_ready(s) or s.host.snapshots.update(
        {"SnapshotName-1-1": PACKAGE_READY, "SnapshotUUID-1-1": TAKEN_UUID, "SnapshotDescription-1-1": ""}), {},
     "package-ready snapshot does not carry the expected activated ancestry marker"),
    (lambda s: at_package_ready(s) or setattr(s.host, "snapshots_code", 1), {},
     "package-ready snapshot does not carry the expected activated ancestry marker"),
    (set_info(CurrentSnapshotName="labview2026-q3-pro-runner-ready"), {},
     "source VM is not at the activated-ready or package-ready boundary"),
    (set_info(CurrentSnapshotName=None, CurrentSnapshotUUID=None), {},
     "source VM is not at the activated-ready or package-ready boundary"),
]


@pytest.mark.parametrize("setup, changes, message", REFUSALS)
@pytest.mark.parametrize("mode", [(), ("--run",)], ids=["plan", "run"])
def test_the_packager_refuses(packaging, capsys, setup, changes, message, mode):
    if setup is not None:
        setup(packaging)
    before = outputs(packaging)
    assert packaging.run(*mode, **changes) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("Vagrant box packaging: FAIL: ") and message in captured.err
    assert packaging.host.mutations() == []
    assert outputs(packaging) == before


def test_a_storage_refusal_stops_before_any_verifier(packaging, capsys):
    packaging.host.transport = (0, "usb\n")
    assert packaging.run("--run") == 2
    assert set(packaging.host.programs()) == {"findmnt", "lsblk"}


def test_a_verifier_refusal_stops_before_virtualbox(packaging, capsys):
    packaging.host.verifiers["verify-activation-receipt.py"] = 1
    assert packaging.run("--run") == 2
    assert packaging.host.programs()[-1] == "verify-activation-receipt.py"


@pytest.mark.parametrize("missing", ["--vboxmanage", "--vagrant"])
def test_the_packager_needs_virtualbox_and_vagrant(packaging, capsys, monkeypatch, missing):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert packaging.run("--run", drop=(missing,)) == 127
    assert capsys.readouterr().err == "Vagrant box packaging: FAIL: VBoxManage or Vagrant is missing\n"
    assert packaging.host.mutations() == []
    found = {"VBoxManage": VBOX, "vagrant": VAGRANT}
    monkeypatch.setattr(shutil, "which", lambda name: found.get(name))
    assert packaging.run(drop=(missing,)) == 0


# Packaging failures: exit 1, and the box, receipt, and receipt hash are removed.

def failed_take(state) -> None:
    state.host.take = (1, state.host.info)


def take_not_current(state) -> None:
    state.host.take = (0, vm_info(CurrentSnapshotName=ACTIVATED, CurrentSnapshotUUID=ACTIVATED_UUID))


FAILURES = [
    (failed_take, "package-ready snapshot creation failed", ["take"]),
    (take_not_current, "package-ready snapshot did not become current", ["take"]),
    (lambda s: setattr(s.host, "package", 1), "vagrant package failed", ["take", "package"]),
    (lambda s: setattr(s.host, "box_bytes", b""), "Vagrant did not produce a nonempty box", ["take", "package"]),
    (lambda s: setattr(s.host, "box_verifier", 2), "Vagrant box receipt verification failed", ["take", "package"]),
    (lambda s: setattr(s.host, "wrapper", WRAPPER + "config.vm.provision 'shell'\n"),
     "Vagrant box receipt verification failed", ["take", "package"]),
]


@pytest.mark.parametrize("setup, message, mutations", FAILURES)
def test_a_packaging_failure_removes_the_outputs(packaging, capsys, setup, message, mutations):
    setup(packaging)
    assert packaging.run("--run") == 1
    assert f"Vagrant box packaging: FAIL: {message}\n" in capsys.readouterr().err
    assert ["take" if "take" in argv else argv[1] for argv in packaging.host.mutations()] == mutations
    assert outputs(packaging) == {}


def test_the_real_box_verifier_names_a_changed_wrapper(packaging, capsys):
    packaging.host.wrapper = WRAPPER.replace("080027ABCDEF", "not-a-mac")
    assert packaging.run("--run") == 1
    assert "Vagrant box receipt: FAIL: generated Vagrantfile contains unexpected behavior" in capsys.readouterr().err


@pytest.mark.parametrize("hangs, signals", [(1, [signal.SIGTERM]), (2, [signal.SIGTERM, signal.SIGKILL])])
def test_a_hung_vagrant_is_stopped(packaging, capsys, hangs, signals):
    packaging.host.hangs = hangs
    assert packaging.run("--run") == 1
    assert capsys.readouterr().err.endswith("Vagrant box packaging: FAIL: vagrant package failed: timed out\n")
    assert packaging.host.killed == signals
    assert outputs(packaging) == {}


@pytest.mark.xfail(raises=ValueError, strict=True,
                   reason="#195: a taken snapshot without a UUID escapes main() with a traceback")
def test_a_taken_snapshot_without_a_uuid_fails_cleanly(packaging, capsys):
    packaging.host.take = (0, vm_info(CurrentSnapshotName=PACKAGE_READY, CurrentSnapshotUUID=None))
    assert packaging.run("--run") == 1
    assert "Vagrant box packaging: FAIL: badly formed hexadecimal UUID string" in capsys.readouterr().err
