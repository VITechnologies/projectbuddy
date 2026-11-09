"""Lane box (VITechnologies/projectbuddy#183): profile, toolchain, readiness, packager, and receipt."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import runpy
import stat
import sys
import tarfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from lane_box import cli, common, packager, profile as lane_profile, readiness, receipt, toolchain  # noqa: E402
from lane_box.common import LaneBoxError  # noqa: E402

REPOSITORY = SCRIPTS.parent
EXAMPLE = REPOSITORY / "vm" / "ubuntu-lane" / "lane-profile.example.json"
EMBEDDED = REPOSITORY / "vm" / "ubuntu-lane" / "Vagrantfile.box"
APPLIANCE_MANIFEST = REPOSITORY / "vm" / "ubuntu-labview" / "provisioning-manifest.json"
VM_NAME = "lane-base"
VM_UUID = "1a1a1a1a-2222-4333-8444-555555555555"
SNAPSHOT_NAME = "lane-ready"
SNAPSHOT_UUID = "66666666-7777-4888-9999-aaaaaaaaaaaa"
BOOT_ID = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
DISK_UUID = "d15cd15c-0000-4111-8222-333333333333"
FINGERPRINT = "SHA256:" + "A" * 43
NOBLE = {"distribution": "ubuntu", "version": "24.04", "codename": "noble"}
NODE_TOP = "node-v24.19.0-linux-x64"
WRAPPER = (
    "Vagrant::Config.run do |config|\n"
    "  # The contents below were provided by the Packer Vagrant post-processor\n"
    "  # or by vagrant package.\n"
    "  # Do not edit.\n"
    '  config.vm.base_mac = "080027ABCDEF"\nend\n\n'
    "# Load include vagrant file if it exists after the auto-generated\n"
    "# so it can override any of the settings\n"
    'include_vagrantfile = File.expand_path("../include/_Vagrantfile", __FILE__)\n'
    "load include_vagrantfile if File.exist?(include_vagrantfile)\n"
)


# ---------------------------------------------------------------- helpers


def example() -> dict:
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def write_json(path: Path, value: object, mode: int = 0o600) -> Path:
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")
    path.chmod(mode)
    return path


def private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def tar_bytes(entries: list[tuple[str, bytes | None, str | None]], mode: str = "w:xz") -> bytes:
    """Build an archive from (name, file content or None for a directory, symlink target)."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode=mode) as archive:
        for name, content, link in entries:
            info = tarfile.TarInfo(name)
            if link is not None:
                info.type, info.linkname = tarfile.SYMTYPE, link
                archive.addfile(info)
            elif content is None:
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                archive.addfile(info)
            else:
                info.size, info.mode = len(content), 0o755
                archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def node_tarball(top: str = NODE_TOP) -> bytes:
    return tar_bytes([
        (top, None, None),
        (f"{top}/bin", None, None),
        (f"{top}/bin/node", b"#!/bin/sh\n", None),
        (f"{top}/lib/node_modules/npm/bin/npm-cli.js", b"// npm\n", None),
        (f"{top}/bin/npm", None, "../lib/node_modules/npm/bin/npm-cli.js"),
    ])


def artifacts_for(data: dict, node: bytes | None = None) -> dict[str, bytes]:
    found = {}
    for tool in data["toolchain"]:
        source = tool["source"]
        if source["kind"] == "tarball":
            found[source["url"]] = node if node is not None else node_tarball()
        elif source["kind"] == "deb":
            found[source["url"]] = f"deb:{tool['name']}".encode()
    return found


def pinned(data: dict, artifacts: dict[str, bytes]) -> dict:
    for tool in data["toolchain"]:
        if tool["source"]["kind"] != "bundled":
            tool["source"]["sha256"] = hashlib.sha256(artifacts[tool["source"]["url"]]).hexdigest()
    return data


def fetcher(artifacts: dict[str, bytes]):
    def fetch(url: str, destination: Path) -> None:
        if url not in artifacts:
            raise OSError(f"no fixture for {url}")
        destination.write_bytes(artifacts[url])

    return fetch


class FakeRunner:
    OUTPUTS = {
        "node": "v24.19.0\n", "npm": "11.17.0\n", "git": "git version 2.43.0\n",
        "dpkg-query": "20260816ubuntu0.24.04.2",
    }

    def __init__(self, outputs: dict | None = None, apt_code: int = 0) -> None:
        self.outputs = dict(self.OUTPUTS, **(outputs or {}))
        self.apt_code = apt_code
        self.calls: list[list[str]] = []

    def __call__(self, argv, timeout=60, path_prefix=None):
        self.calls.append(list(argv))
        if argv[0] == "apt-get":
            return self.apt_code, ""
        output = self.outputs.get(argv[0])
        return (0, output) if output is not None else (127, "")

    @property
    def apt_calls(self) -> list[list[str]]:
        return [call for call in self.calls if call[0] == "apt-get"]


def guest_root(tmp_path: Path, packages: str = "") -> Path:
    root = tmp_path / "guest"
    state = root / "var/lib/projectbuddy/base-bootstrap-state.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"vm": {"name": VM_NAME, "uuid": VM_UUID.upper()}, "os": NOBLE}), encoding="utf-8")
    boot = root / "proc/sys/kernel/random/boot_id"
    boot.parent.mkdir(parents=True)
    boot.write_text(BOOT_ID + "\n", encoding="utf-8")
    status = root / "var/lib/dpkg/status"
    status.parent.mkdir(parents=True)
    status.write_text(
        "Package: bash\nStatus: install ok installed\nDescription: shell\n continued line\n\n"
        "Package: ni-old-driver\nStatus: deinstall ok config-files\n\n" + packages,
        encoding="utf-8",
    )
    (root / "etc/systemd/system").mkdir(parents=True)
    (root / "etc/systemd/system/ssh.service").write_text("[Unit]\n", encoding="utf-8")
    (root / "home/actor").mkdir(parents=True)
    return root


def base_receipt(**overrides) -> dict:
    value = {
        "schema": "projectbuddy/ubuntu-base-bootstrap@2",
        "os": NOBLE,
        "vm": {"name": VM_NAME, "uuid": VM_UUID},
        "tools": {
            "sshd": {"path": "/usr/sbin/sshd", "version": "1:9.6p1-3ubuntu13"},
            "virtualBoxGuestService": {"path": "/usr/sbin/VBoxService", "version": "7.0.16"},
        },
        "services": {
            "ssh": {"activeState": "active", "enabledState": "enabled"},
            "virtualBoxGuestUtils": {"activeState": "active", "enabledState": "enabled"},
        },
        "sshHostKey": {"algorithm": "ssh-ed25519", "fingerprint": FINGERPRINT},
        "verifiedAt": "2026-11-09T00:00:00+00:00",
        "failures": [],
        "outcome": "PASS",
    }
    value.update(overrides)
    return value


VBOXMANAGE_STUB = """
import json, os, pathlib, shutil, signal, sys, time
path = os.environ["LANE_FAKE_VM"]
state = json.load(open(path, encoding="utf-8"))
mode = os.environ.get("LANE_FAKE_VBOX", "ok")
args = sys.argv[1:]
with open(path + ".log", "a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\\n")
vms = dict(state["clones"])
vms[state["vm"]["UUID"]] = state["vm"]


def save():
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(state, stream)


if args[:2] == ["showmediuminfo", "disk"] and len(args) == 3:
    if args[2] not in state["disks"]:
        sys.exit(1)
    print("UUID:           " + args[2])
    print("Capacity:       %s MBytes" % state["disks"][args[2]])
elif args[:1] == ["showvminfo"] and args[2:] == ["--machinereadable"]:
    found = [vm for vm in vms.values() if args[1] in (vm["name"], vm["UUID"])]
    if not found:
        sys.exit(1)
    for key, value in found[0].items():
        print("%s=%s" % (key, value) if isinstance(value, int) else '"%s"="%s"' % (key, value))
elif args == ["list", "vms"]:
    if mode == "list-fail":
        sys.exit(1)
    for vm in vms.values():
        print('"%s" {%s}' % (vm["name"], vm["UUID"]))
elif args[:1] == ["clonevm"]:
    options = dict(arg[2:].split("=", 1) for arg in args[2:] if "=" in arg)
    folder = pathlib.Path(options["basefolder"]) / options["name"]
    folder.mkdir()
    (folder / (options["name"] + ".vbox")).write_text("<VirtualBox/>", encoding="utf-8")
    if mode == "clone-fail":
        sys.exit(1)
    if mode in ("clone-cancel", "clone-die"):
        if mode == "clone-cancel":
            def cancel(signum, frame):
                shutil.rmtree(folder)
                sys.exit(1)
            signal.signal(signal.SIGTERM, cancel)
        with open(os.environ["LANE_FAKE_PIDFILE"], "w", encoding="utf-8") as stream:
            stream.write(str(os.getpid()))
        time.sleep(60)
    flags = ["--mode=machine", "--options=KeepAllMACs,KeepHwUUIDs", "--register"]
    if (args[1] != state["vm"]["UUID"] or options.get("snapshot") != state["vm"]["CurrentSnapshotUUID"]
            or not all(flag in args for flag in flags)):
        sys.exit(4)
    clone = {key: value for key, value in state["vm"].items() if not key.startswith("CurrentSnapshot")}
    clone.update(state["snapshot"], name=options["name"], UUID=options["uuid"])
    clone = {key: value for key, value in clone.items() if value is not None}
    clone["CfgFile"] = str(folder / (options["name"] + ".vbox"))
    state["clones"][options["uuid"]] = clone
    save()
elif args[:1] == ["unregistervm"] and args[2:] == ["--delete"]:
    if mode == "unregister-fail" or args[1] not in state["clones"]:
        sys.exit(1)
    shutil.rmtree(pathlib.Path(state["clones"].pop(args[1])["CfgFile"]).parent)
    save()
else:
    sys.exit(2)
"""

VAGRANT_STUB = """
import io, json, os, sys, tarfile, time
mode = os.environ.get("LANE_FAKE_VAGRANT", "ok")
if mode == "fail" or not os.environ.get("VAGRANT_HOME") or not os.environ.get("TMPDIR"):
    sys.exit(3)
args = sys.argv[1:]
state = json.load(open(os.environ["LANE_FAKE_VM"], encoding="utf-8"))
if args[args.index("--base") + 1] not in state["clones"]:
    sys.exit(4)
if mode == "sleep":
    with open(os.environ["LANE_FAKE_PIDFILE"], "w", encoding="utf-8") as stream:
        stream.write(str(os.getpid()))
    time.sleep(60)
output = args[args.index("--output") + 1]
wrapper = json.loads(os.environ["LANE_FAKE_WRAPPER"])
if mode == "tampered":
    wrapper += 'config.vm.provision "shell", inline: "true"\\n'
if mode == "empty":
    open(output, "wb").close()
    sys.exit(0)
with tarfile.open(output, "w:gz") as archive:
    def add(name, data):
        info = tarfile.TarInfo(name)
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    add("./Vagrantfile", wrapper.encode())
    add("./include/_Vagrantfile", open(args[args.index("--vagrantfile") + 1], "rb").read())
    add("./box.ovf", b"<ovf/>")
"""


class Host:
    """A private host layout, a passing guest's evidence, and stub VBoxManage and vagrant."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = private_dir(tmp_path / "host")
        self.evidence = private_dir(self.root / "evidence")
        self.out = private_dir(self.root / "out")
        self.vagrant_home = private_dir(self.root / "vagrant-home")
        self.temp = private_dir(self.root / "temp")
        self.box = self.out / "lane.box"
        self.receipt = self.out / "lane-box-receipt.json"
        self.profile = EXAMPLE
        write_json(self.evidence / "base-bootstrap-receipt.json", base_receipt())
        loaded = lane_profile.load_profile(EXAMPLE)
        readiness.write_readiness(readiness.build_readiness(guest_root(tmp_path), loaded, FakeRunner()), self.evidence)
        bin_dir = private_dir(self.root / "bin")
        self.vboxmanage = self._stub(bin_dir / "VBoxManage", VBOXMANAGE_STUB)
        self.vagrant = self._stub(bin_dir / "vagrant", VAGRANT_STUB)
        self.vm_state = self.root / "vm-state.json"
        self.set_vm()
        monkeypatch.setenv("LANE_FAKE_VM", str(self.vm_state))
        monkeypatch.setenv("LANE_FAKE_WRAPPER", json.dumps(WRAPPER))
        monkeypatch.delenv("LANE_FAKE_VAGRANT", raising=False)
        monkeypatch.delenv("LANE_FAKE_VBOX", raising=False)

    @staticmethod
    def _stub(path: Path, text: str) -> Path:
        path.write_text(f"#!{sys.executable}" + text, encoding="utf-8")
        path.chmod(0o700)
        return path

    def set_vm(self, snapshot: dict | None = None, **overrides) -> None:
        """Write the source VM; ``snapshot`` holds the settings a clone of the snapshot gets instead."""
        hardware = example()["virtualBox"]
        vm = {
            "name": VM_NAME, "UUID": VM_UUID, "VMState": "poweroff",
            "CurrentSnapshotName": SNAPSHOT_NAME, "CurrentSnapshotUUID": SNAPSHOT_UUID,
            "memory": hardware["memoryMb"], "cpus": hardware["cpus"], "vram": hardware["vramMb"],
            "SATA-ImageUUID-0-0": DISK_UUID,
        }
        disks = overrides.pop("disks", {DISK_UUID: hardware["diskGb"] * 1024})
        vm.update(overrides)
        vm = {key: value for key, value in vm.items() if value is not None}
        state = {"vm": vm, "disks": disks, "snapshot": snapshot or {}, "clones": {}}
        self.vm_state.write_text(json.dumps(state), encoding="utf-8")

    def state(self) -> dict:
        return json.loads(self.vm_state.read_text(encoding="utf-8"))

    def calls(self) -> list[list[str]]:
        log = Path(f"{self.vm_state}.log")
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    def clone_dirs(self) -> list[Path]:
        return sorted(self.temp.glob(f"{packager.CLONE_PREFIX}*"))

    def package_args(self, run: bool = False, **overrides) -> list[str]:
        values = {
            "--profile": str(self.profile), "--vm-name": VM_NAME, "--vm-uuid": VM_UUID,
            "--snapshot-name": SNAPSHOT_NAME, "--snapshot-uuid": SNAPSHOT_UUID,
            "--evidence-dir": str(self.evidence), "--output": str(self.box), "--receipt-out": str(self.receipt),
            "--vagrant-home": str(self.vagrant_home), "--temp-dir": str(self.temp),
            "--vboxmanage": str(self.vboxmanage), "--vagrant": str(self.vagrant),
        }
        values.update(overrides)
        argv = ["package"]
        for key, value in values.items():
            if value is not None:
                argv += [key, value]
        return argv + (["--run"] if run else [])

    def verify_args(self, **overrides) -> list[str]:
        values = {
            "--box": str(self.box), "--profile": str(self.profile),
            "--base-receipt": str(self.evidence / "base-bootstrap-receipt.json"),
            "--readiness-receipt": str(self.evidence / readiness.READINESS_FILENAME),
            "--vm-name": VM_NAME, "--vm-uuid": VM_UUID,
            "--snapshot-name": SNAPSHOT_NAME, "--snapshot-uuid": SNAPSHOT_UUID,
        }
        values.update(overrides)
        argv = ["verify-receipt", str(self.receipt)]
        for key, value in values.items():
            if value is not None:
                argv += [key, value]
        return argv

    def outputs(self) -> list[Path]:
        return [path for path in (self.box, self.receipt, Path(f"{self.receipt}.sha256")) if path.exists()]


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Host:
    return Host(tmp_path, monkeypatch)


def built(host: Host, capsys) -> None:
    assert cli.main(host.package_args(run=True)) == 0, capsys.readouterr().err


# ---------------------------------------------------------------- profile


def test_example_profile_is_valid_and_round_trips():
    loaded = lane_profile.load_profile(EXAMPLE)
    assert loaded.name == "vscode-extension-lane"
    assert loaded.sha256 == common.sha256_file(EXAMPLE)
    assert [tool.name for tool in loaded.tools] == ["node", "npm", "git", "ca-certificates"]
    assert loaded.toolchain_block() == example()["toolchain"]
    assert loaded.tools[0].artifact_name() == "node-v24.19.0-linux-x64.tar.xz"


def test_appliance_manifest_is_refused_as_a_lane_profile():
    with pytest.raises(lane_profile.ProfileError) as raised:
        lane_profile.load_profile(APPLIANCE_MANIFEST)
    message = str(raised.value)
    assert "LabVIEW appliance field 'labview'" in message and "LabVIEW appliance field 'vipm'" in message


@pytest.mark.parametrize("mutate, message", [
    (lambda d: d.update(labview={"package": "x"}), "LabVIEW appliance field 'labview'"),
    (lambda d: d.update(actionsRunner={}), "LabVIEW appliance field 'actionsRunner'"),
    (lambda d: d.update(extra=1), "unknown fields extra"),
    (lambda d: d.pop("virtualBox"), "missing virtualBox"),
    (lambda d: d.update(schema="projectbuddy/ubuntu-lane-profile@2"), "schema must be"),
    (lambda d: d.update(name="Lane Profile"), "name must match"),
    (lambda d: d["os"].update(codename="jammy"), "ubuntu 24.04 (noble)"),
    (lambda d: d["os"].update(virtualBoxOsType=""), "virtualBoxOsType"),
    (lambda d: d["os"].update(iso="x"), "os.iso must be an object"),
    (lambda d: d["os"]["iso"].update(url="http://releases.ubuntu.com/x.iso"), "os.iso.url must be an https URL"),
    (lambda d: d["os"]["iso"].update(url=5), "os.iso.url must be an https URL"),
    (lambda d: d["os"]["iso"].update(url="https://user:pw@releases.ubuntu.com/x.iso"), "os.iso.url"),
    (lambda d: d["os"]["iso"].update(sha256="ABC"), "os.iso.sha256"),
    (lambda d: d["os"]["iso"].update(filename=""), "os.iso.filename"),
    (lambda d: d.update(os=[]), "os must be an object"),
    (lambda d: d["virtualBox"].update(cpus=0), "virtualBox.cpus must be a positive integer"),
    (lambda d: d["virtualBox"].update(memoryMb=True), "virtualBox.memoryMb must be a positive integer"),
    (lambda d: d.update(virtualBox=None), "virtualBox must be an object"),
    (lambda d: d.update(toolchain=[]), "toolchain must be a non-empty list"),
    (lambda d: d["toolchain"].append("node"), "toolchain[4] must be an object"),
    (lambda d: d["toolchain"].append(dict(d["toolchain"][0])), "toolchain names 'node' more than once"),
    (lambda d: d["toolchain"][1]["source"].update({"in": "git"}), "bundled in 'git', which is not a tarball entry"),
    (lambda d: d["toolchain"][1]["source"].update({"in": 3}), "source.in must name a tarball entry"),
    (lambda d: d["toolchain"][0].update(name="Node JS"), "toolchain[0].name must match"),
    (lambda d: d["toolchain"][0].update(version="24 19"), "toolchain[0].version"),
    (lambda d: d["toolchain"][0].update(source="x"), "toolchain[0].source must be an object"),
    (lambda d: d["toolchain"][0]["source"].update(kind="zip"), "kind must be tarball, deb, or bundled"),
    (lambda d: d["toolchain"][0]["source"].update(url="http://nodejs.org/n.tar.xz"), "must be an https URL"),
    (lambda d: d["toolchain"][0]["source"].update(url="https://nodejs.org/n.zip"), "must end with .tar.xz"),
    (lambda d: d["toolchain"][2]["source"].update(url="https://archive.ubuntu.com/git.rpm"), "must end with .deb"),
    (lambda d: d["toolchain"][0]["source"].update(sha256="0" * 63), "source.sha256 must be 64"),
    (lambda d: d["toolchain"][0].update(probe=[]), "probe must be an object"),
    (lambda d: d["toolchain"][0]["probe"].update(argv=[]), "probe.argv must be a non-empty list"),
    (lambda d: d["toolchain"][0]["probe"].update(pattern="v\\S+"), "exactly one capture group"),
    (lambda d: d["toolchain"][0]["probe"].update(pattern="(v)(\\S+)"), "exactly one capture group"),
    (lambda d: d["toolchain"][0]["probe"].update(pattern="(["), "exactly one capture group"),
    (lambda d: d["toolchain"][0]["probe"].update(expected=""), "probe.expected must be a non-empty string"),
])
def test_invalid_profiles_are_refused(mutate, message):
    data = example()
    mutate(data)
    with pytest.raises(lane_profile.ProfileError, match=None) as raised:
        lane_profile.parse_profile(data, "0" * 64)
    assert message in str(raised.value)


@pytest.mark.parametrize("tool", [
    {"name": "labview-cli", "version": "1", "source": {"kind": "deb",
     "url": "https://archive.ubuntu.com/l.deb", "sha256": "0" * 64}},
    {"name": "driver", "version": "1", "source": {"kind": "deb",
     "url": "https://archive.ubuntu.com/pool/ni-daqmx_1_amd64.deb", "sha256": "0" * 64}},
    {"name": "jki", "version": "1", "source": {"kind": "deb",
     "url": "https://packages.example.com/vipm-2026.deb", "sha256": "0" * 64}},
    {"name": "runner", "version": "2.330.0", "source": {"kind": "tarball",
     "url": "https://github.com/actions/runner/releases/download/v2.330.0/actions-runner-linux-x64-2.330.0.tar.gz",
     "sha256": "0" * 64}},
])
def test_labview_vipm_ni_and_runner_components_are_refused(tool):
    data = example()
    tool["probe"] = {"argv": ["x"], "pattern": "(.*)", "expected": "1"}
    data["toolchain"].append(tool)
    with pytest.raises(lane_profile.ProfileError, match="names a LabVIEW, VIPM, or runner component"):
        lane_profile.parse_profile(data, "0" * 64)


def test_downloads_from_ni_are_refused():
    data = example()
    data["toolchain"].append({
        "name": "driver", "version": "1",
        "source": {"kind": "deb", "url": "https://download.ni.com/pool/driver_1_amd64.deb", "sha256": "0" * 64},
        "probe": {"argv": ["x"], "pattern": "(.*)", "expected": "1"},
    })
    with pytest.raises(lane_profile.ProfileError, match="downloads from download.ni.com"):
        lane_profile.parse_profile(data, "0" * 64)


def test_unreadable_and_non_object_profiles_are_refused(tmp_path):
    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    with pytest.raises(lane_profile.ProfileError, match="unreadable"):
        lane_profile.load_profile(broken)
    with pytest.raises(lane_profile.ProfileError, match="must be a JSON object"):
        lane_profile.parse_profile([], "0" * 64)


def test_validate_profile_command(tmp_path, capsys):
    assert cli.main(["validate-profile", str(EXAMPLE)]) == 0
    assert "Lane profile: PASS: vscode-extension-lane" in capsys.readouterr().out
    bad = write_json(tmp_path / "bad.json", dict(example(), labview={}))
    assert cli.main(["validate-profile", str(bad)]) == 2
    assert "Lane profile: FAIL" in capsys.readouterr().err


# ---------------------------------------------------------------- common


def test_common_helpers(tmp_path):
    target = tmp_path / "file.txt"
    common.write_private(target, "x")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    hash_file = tmp_path / "file.txt.sha256"
    hash_file.write_text(common.hash_line(target), encoding="utf-8")
    common.verify_hash_file(hash_file, target)
    hash_file.write_text(f"{'0' * 64}  file.txt\n", encoding="utf-8")
    with pytest.raises(LaneBoxError, match="hash mismatch"):
        common.verify_hash_file(hash_file, target)
    hash_file.write_text(f"{'0' * 64}  other.txt\n", encoding="utf-8")
    with pytest.raises(LaneBoxError, match="does not name"):
        common.verify_hash_file(hash_file, target)
    with pytest.raises(LaneBoxError, match="hash file is missing"):
        common.verify_hash_file(tmp_path / "absent", target)
    with pytest.raises(LaneBoxError, match="not a UUID"):
        common.canonical_uuid("nope", "VM UUID")
    with pytest.raises(LaneBoxError, match="existing absolute path"):
        common.require_private_directory(Path("relative"))
    open_dir = tmp_path / "open"
    open_dir.mkdir(mode=0o755)
    open_dir.chmod(0o755)
    with pytest.raises(LaneBoxError, match="mode 700"):
        common.require_private_directory(open_dir)
    assert common.forbidden_keys({"a": [{"Token": 1}], "password": 2}) == ["password", "token"]


def test_write_private_leaves_no_temporary_file_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(common.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        common.write_private(tmp_path / "x.json", "{}")
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------- toolchain


def install(tmp_path: Path, data: dict, artifacts: dict[str, bytes], run: FakeRunner) -> list[dict]:
    workdir = private_dir(tmp_path / "work")
    return toolchain.install_toolchain(
        lane_profile.parse_profile(data, "0" * 64), workdir=workdir, prefix=tmp_path / "opt",
        bindir=tmp_path / "bin", fetch=fetcher(artifacts), run=run,
    )


def test_toolchain_installs_after_every_digest_matches(tmp_path):
    data = example()
    artifacts = artifacts_for(data)
    run = FakeRunner()
    results = install(tmp_path, pinned(data, artifacts), artifacts, run)
    assert [result["ok"] for result in results] == [True] * 4
    installed = tmp_path / "opt" / "node-24.19.0"
    assert (installed / "bin" / "node").is_file()
    assert os.readlink(tmp_path / "bin" / "node") == str(installed / "bin" / "node")
    assert (tmp_path / "bin" / "npm").resolve() == (installed / "lib/node_modules/npm/bin/npm-cli.js")
    [apt] = run.apt_calls
    assert apt[:4] == ["apt-get", "install", "-y", "--no-install-recommends"]
    assert [Path(item).name for item in apt[4:]] == [
        "git_2.43.0-1ubuntu7.4_amd64.deb", "ca-certificates_20260816ubuntu0.24.04.2_all.deb",
    ]


def test_toolchain_digest_mismatch_installs_nothing(tmp_path):
    data = example()
    artifacts = artifacts_for(data)
    pinned(data, artifacts)
    artifacts[data["toolchain"][3]["source"]["url"]] = b"tampered"
    run = FakeRunner()
    with pytest.raises(LaneBoxError, match="digest mismatch; nothing was installed: ca-certificates"):
        install(tmp_path, data, artifacts, run)
    assert run.calls == []
    assert not (tmp_path / "opt").exists() and not (tmp_path / "bin").exists()


def test_toolchain_download_failure_installs_nothing(tmp_path):
    data = example()
    artifacts = artifacts_for(data)
    pinned(data, artifacts)
    del artifacts[data["toolchain"][2]["source"]["url"]]
    run = FakeRunner()
    with pytest.raises(LaneBoxError, match="cannot download git; nothing was installed"):
        install(tmp_path, data, artifacts, run)
    assert run.calls == [] and not (tmp_path / "opt").exists()


@pytest.mark.parametrize("archive, message", [
    (tar_bytes([("a", None, None), ("b", None, None)]), "exactly one top-level directory"),
    (tar_bytes([("file", b"x", None)]), "exactly one top-level directory"),
    (tar_bytes([("../escape", b"x", None)]), "unsafe or unreadable"),
    (tar_bytes([(NODE_TOP, None, None), (f"{NODE_TOP}/bin/node", None, "/usr/bin/sh")]), "unsafe or unreadable"),
])
def test_toolchain_refuses_unsafe_archives(tmp_path, archive, message):
    data = example()
    artifacts = artifacts_for(data, node=archive)
    with pytest.raises(LaneBoxError, match=message):
        install(tmp_path, pinned(data, artifacts), artifacts, FakeRunner())
    assert not (tmp_path / "opt" / "node-24.19.0").exists()


def test_toolchain_refuses_to_replace_commands_or_installs(tmp_path):
    data = example()
    artifacts = artifacts_for(data)
    pinned(data, artifacts)
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "node").write_text("", encoding="utf-8")
    with pytest.raises(LaneBoxError, match="refusing to replace existing commands") as refused:
        install(tmp_path, data, artifacts, FakeRunner())
    assert type(refused.value) is LaneBoxError
    assert list((tmp_path / "opt").iterdir()) == [] and sorted(os.listdir(tmp_path / "bin")) == ["node"]
    other = tmp_path / "again"
    (other / "opt" / "node-24.19.0").mkdir(parents=True)
    with pytest.raises(LaneBoxError, match="install target already exists") as refused:
        install(other, data, artifacts, FakeRunner())
    assert type(refused.value) is LaneBoxError


def test_toolchain_apt_and_probe_failures_report_the_changed_guest(tmp_path):
    data = example()
    artifacts = artifacts_for(data)
    pinned(data, artifacts)
    changed = "the guest was already changed by: node, git, ca-certificates"
    with pytest.raises(common.ChangedError, match=f"apt-get install failed with exit code 100; {changed}"):
        install(tmp_path / "a", data, artifacts, FakeRunner(apt_code=100))
    with pytest.raises(common.ChangedError, match=f"probes failed after installation: node; {changed}"):
        install(tmp_path / "b", data, artifacts, FakeRunner({"node": "v22.0.0"}))


def test_toolchain_os_errors_are_classified(tmp_path, monkeypatch):
    data = example()
    artifacts = artifacts_for(data)
    loaded = lane_profile.parse_profile(pinned(data, artifacts), "0" * 64)

    def short_read(url, destination):
        raise toolchain.http.client.IncompleteRead(b"")

    with pytest.raises(LaneBoxError, match="cannot download node; nothing was installed") as refused:
        toolchain.install_toolchain(loaded, workdir=private_dir(tmp_path / "w1"), prefix=tmp_path / "o1",
                                    bindir=tmp_path / "b1", fetch=short_read, run=FakeRunner())
    assert type(refused.value) is LaneBoxError
    (tmp_path / "w2" / "node").mkdir(parents=True)
    with pytest.raises(LaneBoxError, match="cannot download node; nothing was installed"):
        toolchain.install_toolchain(loaded, workdir=tmp_path / "w2", prefix=tmp_path / "o2",
                                    bindir=tmp_path / "b2", fetch=fetcher(artifacts), run=FakeRunner())

    def disk_full(self, *args, **kwargs):
        raise OSError(28, "No space left on device")

    with monkeypatch.context() as patched:
        patched.setattr(toolchain.tarfile.TarFile, "extractall", disk_full)
        with pytest.raises(LaneBoxError, match="installation failed; nothing was installed: .*No space") as refused:
            install(tmp_path / "a", data, artifacts, FakeRunner())
    assert type(refused.value) is LaneBoxError and list((tmp_path / "a" / "opt").iterdir()) == []
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "bin").write_text("", encoding="utf-8")
    run = FakeRunner()
    with pytest.raises(common.ChangedError, match="File exists.*; the guest was already changed by: node$"):
        install(tmp_path / "b", data, artifacts, run)
    assert (tmp_path / "b" / "opt" / "node-24.19.0" / "bin" / "node").is_file() and run.apt_calls == []


def test_toolchain_without_debs_skips_apt(tmp_path):
    data = example()
    data["toolchain"] = data["toolchain"][:2]
    artifacts = artifacts_for(data)
    run = FakeRunner()
    install(tmp_path, pinned(data, artifacts), artifacts, run)
    assert run.apt_calls == []


def test_run_command_and_fetch(tmp_path):
    assert toolchain.run_command([sys.executable, "-c", "print('ok')"]) == (0, "ok\n")
    script = "import os; print(os.environ['PATH'].split(os.pathsep)[0])"
    assert toolchain.run_command([sys.executable, "-c", script], path_prefix="/lane/bin") == (0, "/lane/bin\n")
    assert toolchain.run_command([str(tmp_path / "absent")]) == (127, "")
    assert toolchain.run_command([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.2) == (124, "")
    (tmp_path / "noexec").write_text("", encoding="utf-8")
    assert toolchain.run_command([str(tmp_path / "noexec")])[0] == 126
    with pytest.raises(LaneBoxError, match="non-https"):
        toolchain.https_fetch("http://example.com/x", tmp_path / "x")
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")

    class Response(io.BytesIO):
        pass

    original = toolchain.urllib.request.urlopen
    toolchain.urllib.request.urlopen = lambda url, timeout: Response(source.read_bytes())
    try:
        toolchain.https_fetch("https://example.com/x", tmp_path / "fetched")
    finally:
        toolchain.urllib.request.urlopen = original
    assert (tmp_path / "fetched").read_bytes() == b"payload"


def test_install_toolchain_command(tmp_path, capsys):
    data = example()
    artifacts = artifacts_for(data)
    profile_path = write_json(tmp_path / "profile.json", pinned(data, artifacts))
    argv = ["install-toolchain", "--profile", str(profile_path), "--prefix", str(tmp_path / "opt"),
            "--bindir", str(tmp_path / "bin")]
    assert cli.main(argv, fetch=fetcher(artifacts), run=FakeRunner()) == 0
    assert "Lane toolchain: PASS: vscode-extension-lane" in capsys.readouterr().out
    workdir = private_dir(tmp_path / "work")
    tampered = dict(artifacts)
    tampered[data["toolchain"][0]["source"]["url"]] = b"x"
    argv += ["--workdir", str(workdir)]
    assert cli.main(argv, fetch=fetcher(tampered), run=FakeRunner()) == 2
    assert "digest mismatch" in capsys.readouterr().err
    other = ["install-toolchain", "--profile", str(profile_path), "--prefix", str(tmp_path / "opt2"),
             "--bindir", str(tmp_path / "bin2")]
    assert cli.main(other, fetch=fetcher(artifacts), run=FakeRunner(apt_code=100)) == 1
    assert "the guest was already changed by: node, git, ca-certificates" in capsys.readouterr().err


# ---------------------------------------------------------------- readiness


def readiness_for(root: Path, run: FakeRunner | None = None) -> dict:
    return readiness.build_readiness(root, lane_profile.load_profile(EXAMPLE), run or FakeRunner())


def test_clean_guest_passes_readiness(tmp_path):
    result = readiness_for(guest_root(tmp_path))
    assert result["outcome"] == "PASS", result["failures"]
    assert result["vm"] == {"name": VM_NAME, "uuid": VM_UUID}
    assert result["bootId"] == BOOT_ID
    assert result["runnerAbsence"]["skippedRoots"] == ["/dev", "/proc", "/run", "/sys"]
    assert result["labviewAbsence"] == {"paths": [], "packages": [], "scanErrors": []}


def test_skipped_roots_are_not_walked(tmp_path):
    root = guest_root(tmp_path)
    (root / "proc" / ".runner").write_text("{}", encoding="utf-8")
    (root / "run" / "actions.runner.org-repo.lane.service").mkdir(parents=True)
    assert readiness_for(root)["outcome"] == "PASS"


@pytest.mark.parametrize("plant, message", [
    (lambda r: (r / "home/actor/actions-runner").mkdir() or (r / "home/actor/actions-runner/.runner").write_text("{}"),
     "Actions runner configuration is present: /home/actor/actions-runner"),
    (lambda r: (r / "opt/runner").mkdir(parents=True) or (r / "opt/runner/.credentials").write_text("{}"),
     "Actions runner configuration is present: /opt/runner"),
    (lambda r: (r / "etc/systemd/system/actions.runner.org-repo.lane.service").write_text(""),
     "Actions runner unit is present: /etc/systemd/system/actions.runner.org-repo.lane.service"),
    (lambda r: (r / "usr/local/natinst").mkdir(parents=True), "LabVIEW, VIPM, or NI residue is present: /usr/local/natinst"),
])
def test_runner_and_labview_residue_fail_readiness(tmp_path, plant, message):
    root = guest_root(tmp_path)
    plant(root)
    result = readiness_for(root)
    assert result["outcome"] == "FAIL"
    assert message in result["failures"]


def test_installed_labview_packages_fail_readiness(tmp_path):
    root = guest_root(tmp_path, packages=(
        "Package: labview-2026-pro\nStatus: install ok installed\n\n"
        "Package: ni-visa\nStatus: install ok half-configured\n\n"
        "Package: mini-httpd\nStatus: install ok installed\n"
    ))
    result = readiness_for(root)
    assert result["labviewAbsence"]["packages"] == ["labview-2026-pro", "ni-visa"]
    assert result["outcome"] == "FAIL"


def test_incomplete_scans_fail_closed(tmp_path):
    root = guest_root(tmp_path)
    (root / "var/lib/dpkg/status").unlink()
    result = readiness_for(root)
    assert result["labviewAbsence"]["scanErrors"] and "the filesystem scan was incomplete" in result["failures"]


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="root reads every directory")
def test_unreadable_directory_fails_closed(tmp_path):
    root = guest_root(tmp_path)
    locked = root / "home/locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        result = readiness_for(root)
    finally:
        locked.chmod(0o700)
    assert result["runnerAbsence"]["scanErrors"] and result["outcome"] == "FAIL"


@pytest.mark.parametrize("damage, message", [
    (lambda r: (r / "var/lib/projectbuddy/base-bootstrap-state.json").unlink(), "guest identity is unreadable"),
    (lambda r: (r / "var/lib/projectbuddy/base-bootstrap-state.json").write_text('{"vm": {}}'), "no VM name"),
    (lambda r: (r / "var/lib/projectbuddy/base-bootstrap-state.json").write_text(
        json.dumps({"vm": {"name": VM_NAME, "uuid": VM_UUID}, "os": dict(NOBLE, codename="jammy")})), "not ubuntu 24.04"),
    (lambda r: (r / "proc/sys/kernel/random/boot_id").write_text("nope"), "boot id is not a UUID"),
])
def test_identity_problems_fail_readiness(tmp_path, damage, message):
    root = guest_root(tmp_path)
    damage(root)
    result = readiness_for(root)
    assert result["vm"] is None and result["outcome"] == "FAIL"
    assert any(message in failure for failure in result["failures"])


def test_probe_failure_fails_readiness(tmp_path):
    result = readiness_for(guest_root(tmp_path), FakeRunner({"git": "git version 2.39.0"}))
    assert "toolchain probe failed: git" in result["failures"]


def test_readiness_command(tmp_path, capsys, monkeypatch):
    root = guest_root(tmp_path)
    output = private_dir(tmp_path / "evidence")
    argv = ["readiness", "--profile", str(EXAMPLE), "--root", str(root), "--output-dir", str(output)]
    assert cli.main(argv, run=FakeRunner()) == 0
    written = output / readiness.READINESS_FILENAME
    assert stat.S_IMODE(written.stat().st_mode) == 0o600
    common.verify_hash_file(output / f"{readiness.READINESS_FILENAME}.sha256", written)
    (root / "usr/local/natinst").mkdir(parents=True)
    assert cli.main(argv, run=FakeRunner()) == 1
    assert "Lane readiness: FAIL: LabVIEW" in capsys.readouterr().err
    assert cli.main(["readiness", "--profile", str(EXAMPLE), "--root", "relative"], run=FakeRunner()) == 2
    assert cli.main(argv[:-1] + ["relative"], run=FakeRunner()) == 2


# ---------------------------------------------------------------- packager


def test_dry_run_prints_the_plan_and_changes_nothing(host, capsys):
    assert cli.main(host.package_args()) == 0
    out = capsys.readouterr().out
    assert out.startswith("Lane box packaging: PLAN\n")
    assert f"clonevm {VM_UUID} --snapshot={SNAPSHOT_UUID} --mode=machine" in out
    assert f"--name=lane-pack-<new-uuid> --uuid=<new-uuid> --basefolder={host.temp} --register" in out
    assert f"--base <new-uuid> --output {host.box}" in out and "unregistervm <new-uuid> --delete" in out
    assert f"snapshot={SNAPSHOT_NAME} ({SNAPSHOT_UUID})" in out and f"checked_boot_id={BOOT_ID}" in out
    assert host.outputs() == [] and host.clone_dirs() == []
    assert [call[0] for call in host.calls()] == ["showvminfo", "showmediuminfo"]


def test_run_packages_and_writes_a_verifiable_receipt(host, capsys):
    built(host, capsys)
    assert host.state()["clones"] == {} and host.clone_dirs() == []
    commands = [call[0] for call in host.calls()]
    assert commands.count("clonevm") == 1 and commands.count("unregistervm") == 1
    # The source VM is only read and cloned; Vagrant packaged the clone, which was then deleted.
    assert all(call[0] in ("showvminfo", "clonevm") for call in host.calls() if VM_UUID in call or VM_NAME in call)
    [clone] = [call[1] for call in host.calls() if call[0] == "unregistervm"]
    assert clone != VM_UUID and ["showvminfo", clone, "--machinereadable"] in host.calls()
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in host.outputs())
    assert len(host.outputs()) == 3
    common.verify_hash_file(Path(f"{host.receipt}.sha256"), host.receipt)
    value = json.loads(host.receipt.read_text(encoding="utf-8"))
    assert value["schema"] == "projectbuddy/ubuntu-lane-box@1" and value["outcome"] == "PASS"
    assert value["source"] == {"vm": {"name": VM_NAME, "uuid": VM_UUID},
                               "snapshot": {"name": SNAPSHOT_NAME, "uuid": SNAPSHOT_UUID}}
    assert value["sshHostKey"] == {"algorithm": "ssh-ed25519", "fingerprint": FINGERPRINT}
    assert value["runnerAbsence"]["checkedBootId"] == BOOT_ID
    assert value["virtualBox"] == example()["virtualBox"]
    assert [tool["observed"] for tool in value["toolchain"]] == ["24.19.0", "11.17.0", "2.43.0", "20260816ubuntu0.24.04.2"]
    assert value["provenanceSha256"]["embeddedVagrantfile"] == common.sha256_file(EMBEDDED)
    capsys.readouterr()
    assert cli.main(host.verify_args()) == 0
    assert "Lane box receipt: PASS: lane.box" in capsys.readouterr().out
    assert cli.main(host.verify_args(**{"--vm-name": None, "--vm-uuid": None,
                                        "--snapshot-name": None, "--snapshot-uuid": None})) == 0


@pytest.mark.parametrize("vm, message", [
    ({"CurrentSnapshotName": "older", "CurrentSnapshotUUID": VM_UUID}, "is not the current snapshot"),
    ({"CurrentSnapshotUUID": BOOT_ID}, "is not the current snapshot"),
    ({"VMState": "running"}, "must be powered off"),
    ({"UUID": BOOT_ID}, "name or UUID does not match"),
])
def test_vm_state_refusals(host, capsys, vm, message):
    host.set_vm(**vm)
    assert cli.main(host.package_args(run=True)) == 2
    assert message in capsys.readouterr().err
    assert host.outputs() == []


@pytest.mark.parametrize("vm, message", [
    ({"memory": 4096}, "memoryMb is 4096, the profile declares 8192"),
    ({"cpus": 2, "vram": 16}, "cpus is 2, the profile declares 4; vramMb is 16, the profile declares 128"),
    ({"SATA-ImageUUID-0-0": None}, "no disk is attached at SATA port 0"),
    ({"disks": {DISK_UUID: 20480}}, "disk is 20480 MB, the profile declares 40960 MB"),
    ({"disks": {}}, "cannot read the VM disk"),
    ({"disks": {DISK_UUID: "unknown"}}, "cannot read the VM disk capacity"),
    ({"SATA-ImageUUID-0-0": "none"}, "disk UUID is not a UUID"),
])
def test_vm_hardware_must_match_the_profile(host, capsys, vm, message):
    host.set_vm(**vm)
    assert cli.main(host.package_args(run=True)) == 2
    err = capsys.readouterr().err
    assert message in err
    assert host.outputs() == []


def test_hardware_change_between_plan_and_package_is_refused(host, capsys, monkeypatch):
    calls = []
    original = packager.Hypervisor.vm_info

    def drifting(self, vm):
        calls.append(vm)
        if len(calls) == 2:
            host.set_vm(memory=16384)
        return original(self, vm)

    monkeypatch.setattr(packager.Hypervisor, "vm_info", drifting)
    assert cli.main(host.package_args(run=True)) == 2
    assert "memoryMb is 16384" in capsys.readouterr().err and host.outputs() == []
    assert "clonevm" not in [call[0] for call in host.calls()]


def test_snapshot_hardware_is_checked_on_the_clone(host, capsys):
    host.set_vm(snapshot={"memory": 4096})
    assert cli.main(host.package_args(run=True)) == 1
    err = capsys.readouterr().err
    assert "packaging failed; outputs were removed: VM hardware does not match" in err
    assert "memoryMb is 4096" in err and "may remain" not in err
    assert host.outputs() == [] and host.state()["clones"] == {} and host.clone_dirs() == []
    assert "package" not in [call[0] for call in host.calls()]


def test_missing_base_receipt_is_refused(host, capsys):
    (host.evidence / "base-bootstrap-receipt.json").unlink()
    assert cli.main(host.package_args(run=True)) == 2
    assert "base receipt is missing" in capsys.readouterr().err
    assert host.outputs() == []


@pytest.mark.parametrize("overrides, message", [
    ({"outcome": "FAIL"}, "base receipt: outcome is not PASS"),
    ({"schema": "projectbuddy/ubuntu-base-bootstrap@1"}, "base receipt: unexpected schema"),
    ({"vm": {"name": VM_NAME, "uuid": BOOT_ID}}, "base receipt: VM UUID does not match"),
    ({"vm": {"name": VM_NAME, "uuid": "x"}}, "base receipt: VM UUID is invalid"),
    ({"sshHostKey": {"algorithm": "ssh-rsa", "fingerprint": "nope"}}, "SSH host fingerprint is invalid"),
    ({"tools": {}}, "sshd path is missing"),
    ({"services": {"ssh": {"activeState": "failed"}}}, "ssh is not active"),
])
def test_invalid_base_receipt_is_refused(host, capsys, overrides, message):
    write_json(host.evidence / "base-bootstrap-receipt.json", base_receipt(**overrides))
    assert cli.main(host.package_args(run=True)) == 2
    assert message in capsys.readouterr().err


def test_readiness_problems_are_refused(host, tmp_path, capsys):
    target = host.evidence / readiness.READINESS_FILENAME
    target.write_text(target.read_text(encoding="utf-8") + " ", encoding="utf-8")
    assert cli.main(host.package_args(run=True)) == 2
    assert "hash mismatch: lane-readiness-receipt.json" in capsys.readouterr().err
    dirty = guest_root(tmp_path / "dirty")
    (dirty / "etc/systemd/system/actions.runner.org-repo.lane.service").write_text("", encoding="utf-8")
    (dirty / "usr/local/natinst").mkdir(parents=True)
    readiness.write_readiness(readiness_for(dirty), host.evidence)
    assert cli.main(host.package_args(run=True)) == 2
    err = capsys.readouterr().err
    assert "an Actions runner unit is present" in err and "LabVIEW, VIPM, or NI residue is present" in err
    other = write_json(host.root / "other-profile.json", dict(example(), name="other-lane"))
    readiness.write_readiness(readiness_for(guest_root(tmp_path / "clean")), host.evidence)
    assert cli.main(host.package_args(run=True, **{"--profile": str(other)})) == 2
    assert "was produced for a different lane profile" in capsys.readouterr().err
    assert host.outputs() == []


@pytest.mark.parametrize("overrides, message", [
    ({"--vm-name": "bad name"}, "VM name is invalid"),
    ({"--snapshot-name": "bad/name"}, "snapshot name is invalid"),
    ({"--snapshot-uuid": "nope"}, "snapshot UUID is not a UUID"),
    ({"--output": "relative.box"}, "output paths must be absolute"),
    ({"--embedded-vagrantfile": "/nonexistent/Vagrantfile.box"}, "embedded Vagrantfile is missing"),
])
def test_input_refusals(host, capsys, overrides, message):
    assert cli.main(host.package_args(run=True, **overrides)) == 2
    assert message in capsys.readouterr().err


def test_custody_refusals(host, capsys):
    host.temp.chmod(0o755)
    assert cli.main(host.package_args(run=True)) == 2
    assert "mode 700 or stricter" in capsys.readouterr().err
    host.temp.chmod(0o700)
    host.box.write_bytes(b"old")
    assert cli.main(host.package_args(run=True)) == 2
    assert "output already exists" in capsys.readouterr().err
    host.box.unlink()
    (host.evidence / "base-bootstrap-receipt.json").chmod(0o644)
    assert cli.main(host.package_args(run=True)) == 2
    assert "base receipt must have mode 600" in capsys.readouterr().err


def test_unreadable_vm_is_refused(host, capsys):
    assert cli.main(host.package_args(**{"--vm-name": "other-vm"})) == 2
    err = capsys.readouterr().err
    assert "readiness receipt: VM identity does not match" in err or "cannot read VM identity" in err
    hypervisor = packager.Hypervisor(str(host.root / "absent"), str(host.vagrant))
    with pytest.raises(LaneBoxError, match="cannot read VM identity"):
        hypervisor.vm_info(VM_NAME)
    with pytest.raises(LaneBoxError, match="cannot read VM identity"):
        packager.Hypervisor(str(host.vboxmanage), str(host.vagrant)).vm_info("other-vm")


@pytest.mark.parametrize("mode, message", [
    ("fail", "vagrant package failed with exit code 3"),
    ("empty", "did not produce a nonempty box"),
    ("tampered", "generated Vagrantfile contains unexpected behavior"),
])
def test_packaging_failures_remove_outputs(host, capsys, monkeypatch, mode, message):
    monkeypatch.setenv("LANE_FAKE_VAGRANT", mode)
    assert cli.main(host.package_args(run=True)) == 1
    err = capsys.readouterr().err
    assert "packaging failed; outputs were removed" in err and message in err
    assert host.outputs() == [] and host.state()["clones"] == {} and host.clone_dirs() == []


def test_failed_clone_leaves_no_directory(host, capsys, monkeypatch):
    monkeypatch.setenv("LANE_FAKE_VBOX", "clone-fail")
    assert cli.main(host.package_args(run=True)) == 1
    err = capsys.readouterr().err
    assert "packaging failed; outputs were removed: VBoxManage clonevm failed with exit code 1" in err
    assert "may remain" not in err and host.clone_dirs() == [] and host.outputs() == []


@pytest.mark.parametrize("vbox, vagrant, message", [
    ("unregister-fail", "ok", "VBoxManage unregistervm failed with exit code 1"),
    ("list-fail", "ok", "cannot list the registered VMs"),
    ("unregister-fail", "fail", "vagrant package failed with exit code 3; the temporary clone"),
])
def test_a_clone_that_cannot_be_removed_is_reported(host, capsys, monkeypatch, vbox, vagrant, message):
    monkeypatch.setenv("LANE_FAKE_VBOX", vbox)
    monkeypatch.setenv("LANE_FAKE_VAGRANT", vagrant)
    assert cli.main(host.package_args(run=True)) == 1
    err = capsys.readouterr().err
    [clone] = host.state()["clones"]
    assert message in err and f"the temporary clone lane-pack-{clone} ({clone}) may remain" in err
    assert f"VBoxManage unregistervm {clone} --delete, then remove {host.temp / ('lane-pack-' + clone)}" in err
    assert host.outputs() == []


def _terminate_when_started(pidfile: Path) -> None:
    """Send this process SIGTERM once the stub has written its PID file."""
    import signal
    import threading
    import time

    def terminate() -> None:
        deadline = time.monotonic() + 20
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=terminate, daemon=True).start()


def test_an_interrupt_stops_vagrant_and_removes_the_clone(host, capsys, monkeypatch):
    import signal

    pidfile = host.root / "vagrant.pid"
    monkeypatch.setenv("LANE_FAKE_VAGRANT", "sleep")
    monkeypatch.setenv("LANE_FAKE_PIDFILE", str(pidfile))
    before = signal.getsignal(signal.SIGTERM)
    _terminate_when_started(pidfile)
    assert cli.main(host.package_args(run=True)) == 1
    err = capsys.readouterr().err
    assert "packaging failed; outputs were removed: interrupted" in err and "may remain" not in err
    assert signal.getsignal(signal.SIGTERM) is before
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text(encoding="utf-8")), 0)
    assert host.outputs() == [] and host.state()["clones"] == {} and host.clone_dirs() == []


def test_an_interrupted_clone_that_cancels_is_removed(host, capsys, monkeypatch):
    pidfile = host.root / "clone.pid"
    monkeypatch.setenv("LANE_FAKE_VBOX", "clone-cancel")
    monkeypatch.setenv("LANE_FAKE_PIDFILE", str(pidfile))
    _terminate_when_started(pidfile)
    assert cli.main(host.package_args(run=True)) == 1
    err = capsys.readouterr().err
    assert "packaging failed; outputs were removed: interrupted" in err and "may remain" not in err
    assert host.outputs() == [] and host.state()["clones"] == {} and host.clone_dirs() == []
    assert [call[0] for call in host.calls()][-2:] == ["clonevm", "list"]


def test_an_interrupted_clone_that_dies_is_reported(host, capsys, monkeypatch):
    pidfile = host.root / "clone.pid"
    monkeypatch.setenv("LANE_FAKE_VBOX", "clone-die")
    monkeypatch.setenv("LANE_FAKE_PIDFILE", str(pidfile))
    _terminate_when_started(pidfile)
    assert cli.main(host.package_args(run=True)) == 1
    err = capsys.readouterr().err
    clone = re.search(r"lane-pack-([0-9a-f-]{36}) ", err).group(1)
    assert "packaging failed; outputs were removed: interrupted" in err
    assert (f"the temporary clone lane-pack-{clone} ({clone}) may remain" in err
            and "VBoxManage clonevm stopped before VirtualBox confirmed the cancel" in err)
    assert host.outputs() == [] and host.clone_dirs() == []


def test_unexpected_errors_still_clean_up(host, monkeypatch):
    monkeypatch.setattr(packager, "build_box_receipt", lambda **kwargs: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        cli.main(host.package_args(run=True))
    assert host.outputs() == [] and host.state()["clones"] == {} and host.clone_dirs() == []


def test_an_existing_clone_directory_is_refused(host, capsys, monkeypatch):
    fixed = "0c0c0c0c-1111-4222-8333-444444444444"
    monkeypatch.setattr(packager.uuid, "uuid4", lambda: fixed)
    (host.temp / f"lane-pack-{fixed}").mkdir()
    assert cli.main(host.package_args(run=True)) == 2
    assert "temporary clone directory already exists" in capsys.readouterr().err
    assert "clonevm" not in [call[0] for call in host.calls()]


def test_vm_change_between_plan_and_package_is_caught(host, capsys, monkeypatch):
    calls = []
    original = packager.Hypervisor.vm_info

    def drifting(self, vm):
        calls.append(vm)
        if len(calls) == 2:
            host.set_vm(VMState="running")
        return original(self, vm)

    monkeypatch.setattr(packager.Hypervisor, "vm_info", drifting)
    assert cli.main(host.package_args(run=True)) == 2
    assert "must be powered off" in capsys.readouterr().err and host.outputs() == []
    assert calls == [VM_NAME, VM_UUID]


def test_missing_tools_exit_127(host, capsys, monkeypatch):
    monkeypatch.setenv("PATH", str(host.temp))
    assert cli.main(host.package_args(**{"--vboxmanage": None, "--vagrant": None})) == 127
    assert "VBoxManage or Vagrant is missing" in capsys.readouterr().err
    assert cli.main(host.package_args(run=True, **{"--vagrant": str(host.root / "absent")})) == 127
    host.vboxmanage.chmod(0o600)
    assert cli.main(host.package_args(run=True)) == 127
    assert host.calls() == []


def test_package_timeout_kills_the_process_group(host):
    hypervisor = packager.Hypervisor(str(host.vboxmanage), str(host.vagrant))
    with pytest.raises(LaneBoxError, match="timed out"):
        hypervisor.run_checked([sys.executable, "-c", "import time; time.sleep(30)"], dict(os.environ),
                               "vagrant package", 0.5)


def test_package_timeout_escalates_to_sigkill(host, monkeypatch):
    signals = []
    monkeypatch.setattr(packager.os, "killpg", lambda pid, sig: signals.append(sig) or os.kill(pid, 9))

    class Stubborn:
        pid = 0

        def __init__(self, *args, **kwargs):
            self.waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits < 3:
                raise packager.subprocess.TimeoutExpired("x", timeout)
            return -9

    monkeypatch.setattr(packager.subprocess, "Popen", Stubborn)
    monkeypatch.setattr(packager.os, "killpg", lambda pid, sig: signals.append(sig))
    with pytest.raises(LaneBoxError, match="timed out"):
        packager.Hypervisor("v", "g").run_checked(["x"], {}, "x", 1)
    assert signals == [packager.signal.SIGTERM, packager.signal.SIGKILL]


def test_an_interrupt_is_not_masked_when_the_group_is_gone(monkeypatch):
    class Exiting:
        pid = 0

        def __init__(self, *args, **kwargs):
            self.waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                raise KeyboardInterrupt
            return 0

    def gone(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(packager.subprocess, "Popen", Exiting)
    monkeypatch.setattr(packager.os, "killpg", gone)
    with pytest.raises(KeyboardInterrupt):
        packager.Hypervisor("v", "g").run_checked(["x"], {}, "x", 1)


def test_a_second_interrupt_kills_the_group_at_once(monkeypatch):
    signals = []

    class Slow:
        pid = 0

        def __init__(self, *args, **kwargs):
            self.waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits <= 2:
                raise KeyboardInterrupt
            return -9

    monkeypatch.setattr(packager.subprocess, "Popen", Slow)
    monkeypatch.setattr(packager.os, "killpg", lambda pid, sig: signals.append(sig))
    hypervisor = packager.Hypervisor("v", "g")
    with pytest.raises(KeyboardInterrupt):
        hypervisor.run_checked(["x"], {}, "x", 1)
    assert signals == [packager.signal.SIGTERM, packager.signal.SIGKILL] and hypervisor.stop_unconfirmed


@pytest.mark.parametrize("returncode, unconfirmed", [(1, False), (-15, True)])
def test_a_stop_is_confirmed_only_when_the_child_exits_by_itself(monkeypatch, returncode, unconfirmed):
    class Stopped:
        pid = 0

        def __init__(self, *args, **kwargs):
            self.waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                raise packager.subprocess.TimeoutExpired("x", timeout)
            return returncode

    monkeypatch.setattr(packager.subprocess, "Popen", Stopped)
    monkeypatch.setattr(packager.os, "killpg", lambda pid, sig: None)
    hypervisor = packager.Hypervisor("v", "g")
    with pytest.raises(LaneBoxError, match="timed out"):
        hypervisor.run_checked(["x"], {}, "x", 1)
    assert hypervisor.stop_unconfirmed is unconfirmed


# ---------------------------------------------------------------- receipt verification


def test_tampered_box_and_receipt_fail_verification(host, capsys):
    built(host, capsys)
    capsys.readouterr()
    value = json.loads(host.receipt.read_text(encoding="utf-8"))
    tampered = dict(value, toolchain=value["toolchain"][:1])
    write_json(host.receipt, tampered)
    assert cli.main(host.verify_args()) == 2
    assert "receipt does not match its inputs: toolchain" in capsys.readouterr().err
    write_json(host.receipt, dict(value, password="x"))
    assert cli.main(host.verify_args()) == 2
    assert "forbidden fields present: password" in capsys.readouterr().err
    write_json(host.receipt, value)
    with host.box.open("ab") as stream:
        stream.write(b"\0")
    assert cli.main(host.verify_args()) == 2
    assert "receipt does not match its inputs: box" in capsys.readouterr().err


def test_verification_identity_and_custody_checks(host, capsys):
    built(host, capsys)
    capsys.readouterr()
    assert cli.main(host.verify_args(**{"--snapshot-uuid": VM_UUID})) == 2
    assert "source snapshot does not match" in capsys.readouterr().err
    assert cli.main(host.verify_args(**{"--vm-name": "other"})) == 2
    assert "source VM does not match" in capsys.readouterr().err
    assert cli.main(host.verify_args(**{"--vm-uuid": None})) == 2
    assert "go together" in capsys.readouterr().err
    host.box.chmod(0o644)
    assert cli.main(host.verify_args()) == 2
    assert "box mode must be 600" in capsys.readouterr().err
    host.box.chmod(0o600)
    other = host.root / "Vagrantfile.other"
    other.write_text("Vagrant.configure(\"2\") do |config|\nend\n", encoding="utf-8")
    assert cli.main(host.verify_args(**{"--embedded-vagrantfile": str(other)})) == 2
    assert "does not match the embedded Vagrantfile" in capsys.readouterr().err


def test_verification_rejects_bad_source_identity(host, capsys):
    built(host, capsys)
    value = json.loads(host.receipt.read_text(encoding="utf-8"))
    value["source"] = {"vm": {"name": "bad name", "uuid": SNAPSHOT_UUID.upper()}, "snapshot": {"name": SNAPSHOT_NAME, "uuid": "x"}}
    write_json(host.receipt, value)
    with pytest.raises(LaneBoxError) as raised:
        receipt.verify_box_receipt(
            host.receipt, box=host.box, profile_path=EXAMPLE,
            base_receipt=host.evidence / "base-bootstrap-receipt.json",
            readiness_receipt=host.evidence / readiness.READINESS_FILENAME, embedded_vagrantfile=EMBEDDED,
        )
    message = str(raised.value)
    assert "name is invalid" in message and "VM UUID is not canonical" in message
    assert "snapshot UUID is not a UUID" in message


def test_verification_reports_missing_inputs(host, tmp_path, capsys):
    built(host, capsys)
    capsys.readouterr()
    assert cli.main(host.verify_args(**{"--box": str(tmp_path / "absent.box")})) == 2
    assert "box is missing" in capsys.readouterr().err
    assert cli.main(host.verify_args(**{"--embedded-vagrantfile": str(tmp_path / "absent")})) == 2
    assert "embedded Vagrantfile is missing" in capsys.readouterr().err
    write_json(host.root / "list.json", [])
    assert cli.main(host.verify_args(**{"--base-receipt": str(host.root / "list.json")})) == 2
    assert "base receipt must be a JSON object" in capsys.readouterr().err
    (host.root / "broken.json").write_text("{", encoding="utf-8")
    assert cli.main(host.verify_args(**{"--readiness-receipt": str(host.root / "broken.json")})) == 2
    assert "readiness receipt is unreadable" in capsys.readouterr().err
    assert cli.main(host.verify_args(**{"--readiness-receipt": str(tmp_path / "absent.json")})) == 2
    assert "readiness receipt is missing" in capsys.readouterr().err


@pytest.mark.parametrize("members, message", [
    ([("Vagrantfile", WRAPPER.encode(), None)], "exactly one regular included Vagrantfile"),
    ([("include/_Vagrantfile", b"x", None)], "exactly one regular generated Vagrantfile"),
    ([("./Vagrantfile", WRAPPER.encode(), None), ("Vagrantfile", WRAPPER.encode(), None),
      ("include/_Vagrantfile", b"x", None)], "exactly one regular generated Vagrantfile"),
    ([("Vagrantfile", b"\xff", None), ("include/_Vagrantfile", b"x", None)], "box is unreadable"),
])
def test_box_archive_checks(tmp_path, members, message):
    box = tmp_path / "x.box"
    box.write_bytes(tar_bytes(members, mode="w:gz"))
    with pytest.raises(LaneBoxError, match=message):
        receipt.check_box_archive(box)
    box.write_bytes(b"not a tar")
    with pytest.raises(LaneBoxError, match="box is unreadable"):
        receipt.check_box_archive(box)


def test_readiness_receipt_checks_report_each_problem():
    loaded = lane_profile.load_profile(EXAMPLE)
    errors = receipt.check_readiness_receipt({"runnerAbsence": [], "labviewAbsence": None, "toolchain": {}},
                                             loaded, VM_NAME, VM_UUID)
    for expected in ("unexpected schema", "boot id is invalid", "an Actions runner configuration is present",
                     "LabVIEW, VIPM, or NI residue is present", "toolchain probes do not match the profile"):
        assert f"readiness receipt: {expected}" in errors


def test_entry_script_runs_the_cli(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["Invoke-LaneBox.py", "validate-profile", str(EXAMPLE)])
    with pytest.raises(SystemExit) as raised:
        runpy.run_path(str(SCRIPTS / "Invoke-LaneBox.py"), run_name="__main__")
    assert raised.value.code == 0
    assert "Lane profile: PASS" in capsys.readouterr().out


def test_appliance_files_keep_their_own_schema():
    manifest = json.loads(APPLIANCE_MANIFEST.read_text(encoding="utf-8"))
    assert {"labview", "vipm"} <= set(manifest)
    box_policy = (REPOSITORY / "vm" / "ubuntu-labview" / "Vagrantfile.box").read_text(encoding="utf-8")
    assert "virtualbox.memory = 12288" in box_policy
    lane_policy = EMBEDDED.read_text(encoding="utf-8")
    assert 'communicator = "none"' in lane_policy and "auto_correct: false" in lane_policy
    assert 'host_ip: "127.0.0.1"' in lane_policy and "virtualbox.memory" not in lane_policy
