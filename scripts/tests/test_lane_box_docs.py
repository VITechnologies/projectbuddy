"""Lane box: the procedure stays in step with the code."""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lane_box import cli, packager, profile, readiness, receipt  # noqa: E402

REPOSITORY = Path(__file__).resolve().parents[2]
PROCEDURE = (REPOSITORY / "docs" / "vm-provisioning" / "ubuntu-lane-box.md").read_text(encoding="utf-8")
UNWRAPPED = " ".join(PROCEDURE.split())


def table_names(heading: str) -> list[str]:
    """Backticked names in the first column of the table that follows ``heading``."""
    section = PROCEDURE.split(heading, 1)[1].split("\n\n|", 1)[1].split("\n\n", 1)[0]
    rows = [line for line in ("|" + section).splitlines() if line.startswith("| ") and "---" not in line][1:]
    names = [name for row in rows for name in re.findall(r"`([^`]+)`", row.split("|")[1])]
    assert names, heading
    return names


def commands() -> dict[str, argparse.ArgumentParser]:
    parser = cli.build_parser()
    return next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction)).choices


def test_every_command_is_documented():
    assert set(re.findall(r"Invoke-LaneBox\.py ([a-z-]+)", PROCEDURE)) == set(commands())


def test_profile_fields_and_source_kinds_are_documented():
    assert table_names("exactly these fields:") == list(profile.FIELDS)
    assert table_names("The source is\none of:") == [*profile.ARCHIVE_SUFFIXES, "bundled"]
    for suffixes in profile.ARCHIVE_SUFFIXES.values():
        assert all(f"`{suffix}`" in PROCEDURE for suffix in suffixes)
    assert all(f"`{field}`" in PROCEDURE for field in profile.HARDWARE_FIELDS + profile.TOOL_FIELDS
               + profile.PROBE_FIELDS + profile.APPLIANCE_FIELDS)
    assert all(f"`{host}`" in PROCEDURE for host in profile.FORBIDDEN_HOSTS)


def test_readiness_receipt_fields_are_documented(tmp_path):
    def absent(argv, timeout=None, path_prefix=None):
        return 127, ""

    lane = profile.load_profile(REPOSITORY / "vm" / "ubuntu-lane" / "lane-profile.example.json")
    value = readiness.build_readiness(tmp_path, lane, absent)
    assert sorted(table_names("The readiness receipt (")) == sorted(set(value) - {"schema"})
    for block in ("runnerAbsence", "labviewAbsence"):
        row = next(line for line in PROCEDURE.splitlines() if line.startswith(f"| `{block}` |"))
        assert sorted(re.findall(r"`([^`]+)`", row.split("|")[2])) == sorted(value[block])
    toolchain_row = next(line for line in PROCEDURE.splitlines() if line.startswith("| `toolchain` | for each"))
    assert sorted(re.findall(r"`([^`]+)`", toolchain_row)[1:]) == sorted(value["toolchain"][0])


def test_box_receipt_fields_are_documented(tmp_path):
    lane = profile.load_profile(REPOSITORY / "vm" / "ubuntu-lane" / "lane-profile.example.json")
    box = tmp_path / "lane.box"
    box.write_bytes(b"box")
    inputs = dict.fromkeys(("baseReceipt", "embeddedVagrantfile", "profile", "readinessReceipt"), box)
    value = receipt.build_box_receipt(
        profile=lane, vm={}, snapshot={}, box=box, inputs=inputs, base={"sshHostKey": {}},
        readiness={"toolchain": [], "bootId": None},
    )
    assert sorted(table_names("The box receipt (")) == sorted(set(value) - {"schema"})
    provenance = next(line for line in PROCEDURE.splitlines() if line.startswith("| `provenanceSha256` |"))
    expected = receipt.provenance_inputs(Path("p"), Path("b"), Path("r"), Path("e"))
    assert sorted(re.findall(r"`([^`]+)`", provenance.split("|")[2])) == sorted(expected)
    box_row = next(line for line in PROCEDURE.splitlines() if line.startswith("| `box` |"))
    assert sorted(re.findall(r"`([^`]+)`", box_row.split("|")[2])) == sorted(value["box"])


def test_schemas_paths_and_defaults_are_documented():
    for text in (profile.PROFILE_SCHEMA, readiness.READINESS_SCHEMA, receipt.BOX_SCHEMA, receipt.BASE_SCHEMA,
                 readiness.READINESS_FILENAME, packager.BASE_RECEIPT_FILENAME):
        assert f"`{text}`" in PROCEDURE, text
    assert all(f"`/{path}`" in PROCEDURE for path in readiness.LABVIEW_PATHS + readiness.SKIPPED_ROOTS)
    install, scan = commands()["install-toolchain"], commands()["readiness"]
    defaults = {
        "--prefix": install.get_default("prefix"),
        "--bindir": install.get_default("bindir"),
        "--output-dir": scan.get_default("output_dir"),
    }
    for option, default in defaults.items():
        assert f"`{default}` (`{option}`)" in UNWRAPPED, option
    assert packager.CLONE_TIMEOUT == packager.PACKAGE_TIMEOUT == 3600 and "each have a one-hour timeout" in UNWRAPPED
    assert packager.DELETE_TIMEOUT == 600 and "a ten-minute timeout" in UNWRAPPED
    assert packager.STOP_GRACE == 30 and "waits up to 30 seconds, and then sends `SIGKILL`" in UNWRAPPED
    assert cli.EMBEDDED_VAGRANTFILE.relative_to(REPOSITORY).as_posix() in PROCEDURE


def test_exit_codes_are_documented():
    assert table_names("## Exit codes") == ["0", "1", "2", "127"]


def test_packaging_commands_are_documented():
    hypervisor = packager.Hypervisor("VBoxManage", "vagrant")
    checked = packager.Checked(None, {"uuid": "<VM UUID>"}, {"uuid": "<snapshot UUID>"}, None, None, None, None)
    clone = {"name": f"{packager.CLONE_PREFIX}<new UUID>", "uuid": "<clone UUID>"}
    clone_command = hypervisor.clone_command(checked, clone, Path("dir"))
    documented = [arg for arg in clone_command if not arg.startswith(("--name=", "--uuid=", "--basefolder="))]
    assert f"`{' '.join(documented)}`" in UNWRAPPED
    assert f"`{clone['name']}`" in PROCEDURE
    assert f"`{' '.join(hypervisor.delete_command('<clone UUID>'))}`" in PROCEDURE
    request = argparse.Namespace(output="OUT", embedded_vagrantfile=cli.EMBEDDED_VAGRANTFILE)
    package = hypervisor.package_command(request, "<clone UUID>")
    assert " ".join(package[:4]) + " --vagrantfile vm/ubuntu-lane/Vagrantfile.box" in UNWRAPPED
