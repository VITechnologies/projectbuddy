"""Ubuntu base VM builder (VITechnologies/projectbuddy#186): appliance manifest or lane profile."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
BASH = shutil.which("bash") or "/bin/bash"
BUILDER = REPOSITORY / "vm" / "ubuntu-labview" / "build-virtualbox.sh"
MANIFEST = REPOSITORY / "vm" / "ubuntu-labview" / "provisioning-manifest.json"
EXAMPLE = REPOSITORY / "vm" / "ubuntu-lane" / "lane-profile.example.json"
LANE_BOX_PROCEDURE = REPOSITORY / "docs" / "vm-provisioning" / "ubuntu-lane-box.md"
APPLIANCE_PROFILE = "Profile: Ubuntu24_LTS_64 | 12288 MB | 6 CPU | 128 MB VRAM | 80 GB"
LANE_PROFILE = "Profile: Ubuntu24_LTS_64 | 8192 MB | 4 CPU | 128 MB VRAM | 40 GB"
APPLIANCE_NEXT = "upload the manifest, provisioner, and NI key; then run provision-guest.sh as root"
LANE_NEXT = "continue at step 2 of docs/vm-provisioning/ubuntu-lane-box.md (install-toolchain)"
EXISTING_VM = '"lane-test" {11111111-2222-4333-8444-555555555555}'


@pytest.fixture
def tools(tmp_path: Path) -> Path:
    """A PATH directory whose python3 is the interpreter running the tests."""
    directory = tmp_path / "bin"
    directory.mkdir()
    (directory / "python3").symlink_to(sys.executable)
    return directory


def build(tools: Path, tmp_path: Path, *arguments: str, **overrides: str | None) -> subprocess.CompletedProcess:
    env = {
        "PATH": f"{tools}:/usr/bin:/bin",
        "VM_NAME": "lane-test",
        "BASEFOLDER": str(tmp_path / "vms"),
        "GUEST_FULLNAME": "VM Builder",
        "GUEST_HOSTNAME": "vm-builder",
    }
    env.update(overrides)
    env = {name: value for name, value in env.items() if value is not None}
    return subprocess.run([BASH, str(BUILDER), *arguments], env=env, capture_output=True, text=True, timeout=60)


def fake_vboxmanage(tools: Path, listed: str = "") -> None:
    stub = tools / "VBoxManage"
    stub.write_text(f"#!/bin/sh\n[ \"$1 $2\" = 'list vms' ] && printf '%s\\n' '{listed}'\nexit 0\n")
    stub.chmod(0o755)


def lane_copy(tmp_path: Path, **changes) -> Path:
    data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    for dotted, value in changes.items():
        *parents, leaf = dotted.split("__")
        target = data
        for parent in parents:
            target = target[parent]
        target[leaf] = value
    path = tmp_path / "lane-profile.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def manifest_copy(tmp_path: Path, **hardware) -> Path:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    data["virtualBox"].update(hardware)
    path = tmp_path / "provisioning-manifest.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_appliance_dry_run_plans_the_appliance_hardware(tools, tmp_path):
    result = build(tools, tmp_path, "--dry-run", "--no-start")
    assert result.returncode == 0, result.stderr
    assert APPLIANCE_PROFILE in result.stdout
    assert "--memory 12288 --cpus 6 --vram 128 " in result.stdout
    assert "--size 81920 --format VDI" in result.stdout
    assert APPLIANCE_NEXT in result.stdout
    assert "Lane profile:" not in result.stdout and LANE_NEXT not in result.stdout


def test_lane_dry_run_plans_the_profile_hardware(tools, tmp_path):
    result = build(tools, tmp_path, "--dry-run", "--no-start", LANE_PROFILE=str(EXAMPLE))
    assert result.returncode == 0, result.stderr
    digest = hashlib.sha256(EXAMPLE.read_bytes()).hexdigest()
    assert f"Lane profile: vscode-extension-lane ({digest})" in result.stdout
    assert LANE_PROFILE in result.stdout
    assert "--memory 8192 --cpus 4 --vram 128 " in result.stdout
    assert "--size 40960 --format VDI" in result.stdout
    assert "--iso=/path/to/ubuntu-24.04.4-desktop-amd64.iso" in result.stdout
    assert LANE_NEXT in result.stdout
    assert "provision-guest.sh" not in result.stdout and "NI key" not in result.stdout


def test_lane_hardware_comes_from_the_profile(tools, tmp_path):
    profile = lane_copy(tmp_path, virtualBox={"diskGb": 64, "memoryMb": 4096, "cpus": 2, "vramMb": 32})
    result = build(tools, tmp_path, LANE_PROFILE=str(profile))
    assert result.returncode == 0, result.stderr
    assert "Profile: Ubuntu24_LTS_64 | 4096 MB | 2 CPU | 32 MB VRAM | 64 GB" in result.stdout
    assert "--memory 4096 --cpus 2 --vram 32 " in result.stdout
    assert "--size 65536 --format VDI" in result.stdout


@pytest.mark.parametrize(("changes", "message"), [
    ({"labview": {"version": "2025"}}, "carries the LabVIEW appliance field 'labview'"),
    ({"actionsRunner": {}}, "carries the LabVIEW appliance field 'actionsRunner'"),
    ({"virtualBox__cpus": 0}, "virtualBox.cpus must be a positive integer"),
    ({"virtualBox__memoryMb": "8192"}, "virtualBox.memoryMb must be a positive integer"),
    ({"os__codename": "jammy"}, "os must be ubuntu 24.04 (noble)"),
])
def test_an_invalid_lane_profile_is_refused_before_any_plan(tools, tmp_path, changes, message):
    result = build(tools, tmp_path, LANE_PROFILE=str(lane_copy(tmp_path, **changes)))
    assert result.returncode == 2
    assert "lane profile refused: " in result.stderr and message in result.stderr
    assert result.stdout == ""


def test_the_appliance_manifest_is_not_a_lane_profile(tools, tmp_path):
    result = build(tools, tmp_path, LANE_PROFILE=str(MANIFEST))
    assert result.returncode == 2
    assert "schema must be projectbuddy/ubuntu-lane-profile@1" in result.stderr
    assert "carries the LabVIEW appliance field 'labview'" in result.stderr
    assert result.stdout == ""


def test_a_lane_profile_is_not_an_appliance_manifest(tools, tmp_path):
    result = build(tools, tmp_path, PROVISIONING_MANIFEST=str(EXAMPLE))
    assert result.returncode == 2
    assert "manifest schema must be projectbuddy/ubuntu-labview-provisioning@1" in result.stderr
    assert result.stdout == ""


def test_both_sources_are_refused(tools, tmp_path):
    result = build(tools, tmp_path, LANE_PROFILE=str(EXAMPLE), PROVISIONING_MANIFEST=str(MANIFEST))
    assert result.returncode == 2
    assert "set LANE_PROFILE or PROVISIONING_MANIFEST, not both" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize("variable", ["LANE_PROFILE", "PROVISIONING_MANIFEST"])
def test_an_unreadable_source_is_refused(tools, tmp_path, variable):
    result = build(tools, tmp_path, **{variable: str(tmp_path / "missing.json")})
    assert result.returncode == 2
    kind = "lane" if variable == "LANE_PROFILE" else "appliance"
    assert f"{kind} source not readable: " in result.stderr


def test_an_unparseable_manifest_is_refused(tools, tmp_path):
    broken = tmp_path / "provisioning-manifest.json"
    broken.write_text("{", encoding="utf-8")
    result = build(tools, tmp_path, PROVISIONING_MANIFEST=str(broken))
    assert result.returncode == 2 and "manifest is unreadable: " in result.stderr


def test_a_manifest_without_hardware_is_refused(tools, tmp_path):
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    del data["virtualBox"]
    path = tmp_path / "provisioning-manifest.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    result = build(tools, tmp_path, PROVISIONING_MANIFEST=str(path))
    assert result.returncode == 2 and "manifest is missing an os, iso, or virtualBox value: " in result.stderr


@pytest.mark.parametrize("hardware", [{"memoryMb": 8192}, {"cpus": 4}, {"diskGb": 40}, {"vramMb": 64}])
def test_the_appliance_hardware_stays_pinned(tools, tmp_path, hardware):
    result = build(tools, tmp_path, PROVISIONING_MANIFEST=str(manifest_copy(tmp_path, **hardware)))
    assert result.returncode == 2 and "unexpected VirtualBox hardware profile" in result.stderr


def test_appliance_hardware_must_be_integers(tools, tmp_path):
    result = build(tools, tmp_path, PROVISIONING_MANIFEST=str(manifest_copy(tmp_path, diskGb="80")))
    assert result.returncode == 2 and "virtualBox values must be integers" in result.stderr


@pytest.mark.parametrize(("changes", "message"), [
    ({"os__virtualBoxOsType": "Ubuntu_64"}, "unsupported VirtualBox OS type: Ubuntu_64"),
    ({"os__iso__filename": "ubuntu-24.04.3-desktop-amd64.iso"}, "unexpected Ubuntu ISO filename"),
    ({"os__iso__url": "https://mirror.example/ubuntu-24.04.4-desktop-amd64.iso"}, "unexpected Ubuntu ISO URL"),
    ({"os__iso__sha256": "0" * 64}, "unexpected Ubuntu ISO SHA-256"),
])
def test_a_lane_profile_keeps_the_os_and_iso_pins(tools, tmp_path, changes, message):
    result = build(tools, tmp_path, LANE_PROFILE=str(lane_copy(tmp_path, **changes)))
    assert result.returncode == 2 and message in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize("source", ["appliance", "lane"])
@pytest.mark.parametrize(("overrides", "message"), [
    ({"VM_NAME": "lane test"}, "VM_NAME contains unsupported characters"),
    ({"SSH_HOST_PORT": "80"}, "SSH_HOST_PORT must be an integer from 1024 through 65535"),
    ({"SSH_HOST_PORT": "22x"}, "SSH_HOST_PORT must be an integer from 1024 through 65535"),
    ({"GUEST_USER": "Root"}, "GUEST_USER is invalid"),
    ({"GUEST_HOSTNAME": None}, "GUEST_HOSTNAME is required"),
])
def test_identity_refusals_hold_for_both_sources(tools, tmp_path, source, overrides, message):
    lane = {"LANE_PROFILE": str(EXAMPLE)} if source == "lane" else {}
    result = build(tools, tmp_path, **lane, **overrides)
    assert result.returncode == 2 and message in result.stderr


def test_a_lane_run_refuses_an_existing_vm(tools, tmp_path):
    fake_vboxmanage(tools, listed=EXISTING_VM)
    result = build(tools, tmp_path, "--run", LANE_PROFILE=str(EXAMPLE))
    assert result.returncode == 3 and "VM already exists: lane-test" in result.stderr


def test_a_lane_run_requires_the_iso(tools, tmp_path):
    fake_vboxmanage(tools)
    result = build(tools, tmp_path, "--run", LANE_PROFILE=str(EXAMPLE))
    assert result.returncode == 3 and "--run requires ISO" in result.stderr


def test_a_lane_run_checks_the_iso_digest(tools, tmp_path):
    fake_vboxmanage(tools)
    iso = tmp_path / "ubuntu-24.04.4-desktop-amd64.iso"
    iso.write_bytes(b"not the ISO")
    password = tmp_path / "guest-password"
    password.write_text("secret\n", encoding="utf-8")
    password.chmod(0o600)
    result = build(tools, tmp_path, "--run", LANE_PROFILE=str(EXAMPLE), ISO=str(iso), GUEST_PASSWORD_FILE=str(password))
    assert result.returncode != 0
    assert "FAILED" in result.stdout + result.stderr
    assert not (tmp_path / "vms").exists()


def test_a_run_requires_vboxmanage(tools, tmp_path):
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "python3").symlink_to(sys.executable)
    (bare / "dirname").symlink_to(shutil.which("dirname"))
    result = build(tools, tmp_path, "--run", LANE_PROFILE=str(EXAMPLE), PATH=str(bare))
    assert result.returncode == 2 and "VBoxManage is required" in result.stderr


def test_usage_names_both_sources(tools, tmp_path):
    result = build(tools, tmp_path, "--help")
    assert result.returncode == 0
    assert "LANE_PROFILE" in result.stdout and "PROVISIONING_MANIFEST" in result.stdout


def test_the_lane_box_procedure_names_the_build_command():
    text = " ".join(LANE_BOX_PROCEDURE.read_text(encoding="utf-8").split())
    assert "LANE_PROFILE=vm/ubuntu-lane/lane-profile.example.json" in text
    assert "vm/ubuntu-labview/build-virtualbox.sh" in text
    assert "accepts only the appliance hardware" not in text
