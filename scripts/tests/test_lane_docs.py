"""Ubuntu lane: the procedure stays in step with the teardown code."""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lane import channel, cli, common, declaration, output_disk, outputs, plan, supervisor, teardown  # noqa: E402
from vagrant_clone import guest as clone_guest  # noqa: E402
from lane_box.toolchain import PROBE_TIMEOUT  # noqa: E402

REPOSITORY = Path(__file__).resolve().parents[2]
PROCEDURE = (REPOSITORY / "docs" / "vm-provisioning" / "ubuntu-lane.md").read_text(encoding="utf-8")
UNWRAPPED = " ".join(PROCEDURE.split())


def table(heading: str) -> list[list[str]]:
    """The cells of the table that follows ``heading``, or that starts with it, without its header row."""
    if heading.startswith("| "):
        section = heading + PROCEDURE.split(heading, 1)[1].split("\n\n", 1)[0]
    else:
        section = "|" + PROCEDURE.split(heading, 1)[1].split("\n\n|", 1)[1].split("\n\n", 1)[0]
    rows = [line for line in section.splitlines() if line.startswith("| ") and "---" not in line][1:]
    assert rows, heading
    return [[cell.strip() for cell in row.strip("|").split("|")] for row in rows]


def names(cell: str) -> list[str]:
    return re.findall(r"`([^`]+)`", cell)


def code_blocks(heading: str, until: str) -> list[str]:
    """The text code blocks between ``heading`` and ``until``, each with its line continuations joined."""
    section = PROCEDURE.split(heading, 1)[1].split(until, 1)[0]
    blocks = re.findall(r"```text\n(.*?)\n```", section, re.S)
    return [" ".join(block.replace("\\\n", " ").split()) for block in blocks]


def commands() -> dict[str, argparse.ArgumentParser]:
    parser = cli.build_parser()
    return next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction)).choices


def test_every_command_and_option_is_documented():
    assert set(re.findall(r"Invoke-Lane\.py ([a-z-]+)", PROCEDURE)) == set(commands())
    for command in commands().values():
        for action in command._actions:
            assert all(f"`{option}`" in PROCEDURE for option in action.option_strings if option not in ("-h", "--help"))


def test_the_allow_list_table_is_the_code():
    rows = table("### The allow-list")
    assert set(names(rows[0][0])) == teardown.POWEROFF_FROM
    assert all(teardown.decide(state) == "poweroff" for state in names(rows[0][0]))
    assert set(names(rows[1][0])) == teardown.DELETE_FROM
    assert all(teardown.decide(state) == "delete" for state in names(rows[1][0]))
    assert names(rows[2][0]) == [teardown.ABSENT]
    refused = re.findall(r"`([a-z]+)`", UNWRAPPED.split("Refused states include", 1)[1].split("any state", 1)[0])
    assert {"livesnapshotting", "onlinesnapshotting"} <= set(refused)
    assert all(teardown.decide(state) is None for state in refused)
    assert sorted(teardown.STOPPING) == ["paused", "running", "stopping"]
    assert "while it is `running`, `paused`, or `stopping`" in UNWRAPPED


def test_the_vboxmanage_commands_are_documented():
    for action in ("poweroff", "delete"):
        assert f"`VBoxManage {' '.join(teardown.command_for(action, '<UUID>'))}`" in PROCEDURE
        assert f"`{' '.join(teardown.command_for(action, '<uuid>'))}`" in UNWRAPPED


def test_receipt_fields_and_outcomes_are_documented():
    assert teardown.TEARDOWN_SCHEMA == "projectbuddy/lane-teardown@1" and f"`{teardown.TEARDOWN_SCHEMA}`" in PROCEDURE
    fields = [names(row[0])[0] for row in table("The teardown receipt (")]
    assert sorted(fields) == sorted(teardown.RECEIPT_FIELDS - {"schema"})
    receipt = teardown.Record(common.target_uuid("0a0a0a0a-1111-4222-8333-444444444444")).receipt("0" * 64)
    rows = {names(row[0])[0]: row[1] for row in table("The teardown receipt (")}
    assert set(names(rows["instance"])) - {"null"} == set(receipt["instance"])
    assert sorted(names(rows["allowList"])) == sorted(receipt["allowList"])
    assert sorted(names(rows["box"])[:2]) == sorted(receipt["box"])
    assert tuple(names(rows["runDirectory"])) == teardown.RUN_DIRECTORY_STATES
    assert tuple(names(row[0])[0] for row in table("| Outcome | Meaning |")) == teardown.OUTCOMES


def test_paths_names_and_timeouts_are_documented():
    assert f"`{common.MACHINE_ID.as_posix()}`" in PROCEDURE
    assert f"`{common.INSTANCE_NAME.pattern}`" in PROCEDURE
    leftover = teardown.leftover_for(Path("/runs/<run directory name>"), "<UUID>")
    assert f"`{leftover.name}`" in PROCEDURE
    assert f"`$XDG_RUNTIME_DIR/{cli.LOCK_DIRECTORY}` (`--lock-dir`)" in UNWRAPPED
    assert teardown.POLL_INTERVAL == 2 and "every 2 seconds" in UNWRAPPED
    assert teardown.POWEROFF_WAIT == 120 and "for up to 120 seconds" in UNWRAPPED
    assert teardown.LOCK_WAIT == 1800 and "waits up to 30 minutes" in UNWRAPPED
    assert teardown.POWEROFF_TIMEOUT == 300 and "300 seconds for `controlvm`" in UNWRAPPED
    assert teardown.DELETE_TIMEOUT == 600 and "600 seconds for `unregistervm`" in UNWRAPPED
    assert teardown.QUERY_TIMEOUT == 120 and "120 seconds for each query" in UNWRAPPED


def test_exit_codes_are_documented():
    assert [row[0] for row in table("## Exit codes")] == ["`0`", "`1`", "`2`", "`127`"]


def test_the_declaration_table_is_the_code():
    assert f"`{declaration.WORKLOAD_SCHEMA}`" in PROCEDURE
    assert tuple(names(row[0])[0] for row in table("### The workload declaration")) == declaration.FIELDS
    listed = re.findall(r"`(\.[a-z]+)`", UNWRAPPED.split("a file name ending in", 1)[1].split("is refused", 1)[0])
    assert listed == [".vdi", ".vmdk", ".vhd", ".vhdx", ".sav", ".vbox"]
    assert all(declaration.FORBIDDEN_OUTPUT.fullmatch(f"disk{suffix.upper()}") for suffix in listed)
    assert declaration.FORBIDDEN_OUTPUT.fullmatch("known_hosts") and "named `known_hosts`" in UNWRAPPED
    assert declaration.SAFE_INTEGER == 2 ** 53 - 1 and "an integer beyond 2^53 - 1" in UNWRAPPED


def test_the_refusal_table_is_the_code():
    assert tuple(names(row[0])[0] for row in table("### Refusals")) == plan.RULES
    assert f"`{plan.RECORD_SUFFIX}` receipt" in PROCEDURE and f"`<instance name>{plan.RECORD_SUFFIX}`" in PROCEDURE


def test_the_plan_fields_are_the_code():
    assert f"The plan (`{plan.PLAN_SCHEMA}`) holds:" in PROCEDURE
    rows = table(f"The plan (`{plan.PLAN_SCHEMA}`) holds:")
    assert ("schema",) + tuple(names(row[0])[0] for row in rows) == plan.PLAN_FIELDS
    step_fields = UNWRAPPED.split("Each step has", 1)[1].split("{uuid}", 1)[0]
    assert all(f"`{field}`" in step_fields for field in plan.STEP_FIELDS)
    phases = UNWRAPPED.split("The phases are", 1)[1].split(";", 1)[0]
    assert tuple(names(phases)) == plan.PHASES
    assert plan.LINK_OFF_FROM == "build"
    assert "its link off from the `build` phase on and never turned on again" in UNWRAPPED
    disk = names(dict((names(row[0])[0], row[1]) for row in rows)["outputDisk"])
    units = names(dict((names(row[0])[0], row[1]) for row in rows)["guestUnits"])
    assert tuple(units) == plan.GUEST_PHASES + ("timeoutSeconds", "null")
    assert set(plan.GUEST_PHASES) < set(plan.PHASES)
    assert "a guest step's own `timeoutSeconds` is `null`" in UNWRAPPED
    assert tuple(disk) == plan.OUTPUT_DISK_FIELDS + ("null",)
    assert plan.output_disk(Path("/run"), plan.MIB + 1)["sizeMb"] == 2
    assert "a fixed-size VDI of `maxOutputBytes` rounded up to whole MiB" in UNWRAPPED
    assert f"`{plan.OUTPUT_DISK_CONTROLLER}` controller at port {plan.OUTPUT_DISK_PORT}" in UNWRAPPED
    assert f"`{plan.UUID_PLACEHOLDER}` in an argv" in UNWRAPPED


def test_the_step_table_is_the_code():
    request = SimpleNamespace(work_dir=Path("/work"), box=Path("/box"), engine=Path("/engine"),
                              records_dir=Path("/records"))
    tool = {"name": "<tool>", "probe": {"argv": ["tool", "--version"]}}
    phase = declaration.Phase(steps=(("run",),), timeout=1)
    workload = SimpleNamespace(toolchain=(tool,), fetch=phase, build=phase, secrets=(), working_directory=".")
    steps = plan.plan_steps(request, SimpleNamespace(vboxmanage="VBoxManage", vagrant="vagrant"), workload,
                            "pb-lane-0123456789ab", "0" * 64)
    for step in steps:
        step["name"] = re.sub(r"^(fetch|build)-1$", r"\1-<n>", step["name"])
    rows = table("| Step | Phase | Runs | What it does |")
    assert [(names(row[0])[0], names(row[1])[0], row[2]) for row in rows] == [
        (step["name"], step["phase"], step["runs"]) for step in steps]
    assert {step["phase"] for step in steps} == set(plan.PHASES)
    documented = {names(row[0])[0]: row[3] for row in rows}
    for step in steps:
        if step["runs"] == "host" and step["argv"][0] == "VBoxManage" and step["name"] != "verify-network-off":
            argv = " ".join(step["argv"]).replace("/work/pb-lane-0123456789ab", "<run directory>")
            assert f"`{argv}`" in documented[step["name"]], step["name"]
    assert [step["name"] for step in steps if step["argv"] and "mediumio" in step["argv"]] == ["read-outputs"]


def test_plan_timeouts_are_documented():
    assert PROBE_TIMEOUT == 60 and "for up to 60 seconds" in UNWRAPPED
    assert plan.HOST_DEADLINE_GRACE == 300
    assert "the fetch timeout plus 300 seconds" in UNWRAPPED and "the build timeout plus 300 seconds" in UNWRAPPED
    assert plan.QUERY_TIMEOUT == 120 and plan.GIT_TIMEOUT == 120
    assert "Host queries time out after 120 seconds, `git` after 120 seconds" in UNWRAPPED
    assert plan.BOX_ADD_TIMEOUT == plan.UP_TIMEOUT == plan.READ_TIMEOUT == 1800
    assert "`box-add`, `up`, and `read-outputs` after 1800 seconds each" in UNWRAPPED


def test_the_output_disk_is_documented():
    assert tuple(names(row[0])[0] for row in table("| Rule | The read fails when |")) == outputs.READ_RULES
    assert f"`{output_disk.RESULT_MEMBER}`" in UNWRAPPED
    assert output_disk.EXIT_LIMIT == 255 and "`exited <code>` (0 to 255)" in UNWRAPPED
    assert output_disk.SIGNAL_LIMIT == 64 and "`killed <signal>` (1 to 64)" in UNWRAPPED
    assert output_disk.RESULT_LIMIT == 16 and "it is longer than 16 bytes" in UNWRAPPED
    assert output_disk.SMALLEST_ARCHIVE == 2048 and "an integer of at least 2048" in UNWRAPPED
    assert output_disk.NAME_LIMIT == 100 and "at most 100 bytes in UTF-8" in UNWRAPPED
    assert f"`{output_disk.LANE_GUEST_MARKER}`" in PROCEDURE and "changes nothing and exits 2" in UNWRAPPED
    properties = UNWRAPPED.split("reads the build unit's", 1)[1].split("with `systemctl show`", 1)[0]
    assert tuple(names(properties)) == output_disk.UNIT_PROPERTIES
    assert output_disk.header("a", 0)[100:108] == b"0000600\0" and "mode 0600, owner and group 0" in UNWRAPPED


def test_the_guest_phases_are_documented():
    modes = next(action for action in supervisor.build_parser()._actions
                 if isinstance(action, argparse._SubParsersAction)).choices
    assert [names(row[0])[0] for row in table("| Mode | Runs as | What it does |")] == list(modes)
    assert supervisor.GUEST_PHASES == plan.GUEST_PHASES == ("fetch", "build")
    assert f"`{supervisor.DIRECTORY}`" in PROCEDURE and f"`{supervisor.SPEC_NAME}`" in PROCEDURE
    assert f"(`{supervisor.SPEC_SCHEMA}`) holds exactly these fields" in UNWRAPPED
    fields = [names(row[0])[0] for row in table(f"`{supervisor.SPEC_SCHEMA}`) holds exactly these fields")]
    assert fields == list(supervisor.SPEC_FIELDS)
    assert all(f"`{field}`" in UNWRAPPED for field in supervisor.PHASE_FIELDS)
    assert f"starts `{supervisor.UNITS['fetch']}`" in UNWRAPPED
    assert f"starts `{supervisor.UNITS['build']}`" in UNWRAPPED
    assert f"in `{supervisor.SUPERVISOR_UNIT}`" in UNWRAPPED
    assert f"transient unit in `{supervisor.SLICE}`" in UNWRAPPED


def test_the_guest_unit_commands_are_the_code():
    blocks = code_blocks("### Guest phases", "### The output disk")
    spec = SimpleNamespace(root="<root>", device="<device>", max_output_bytes="<maxOutputBytes>",
                           outputs=("<output>",), fetch=SimpleNamespace(timeout="<fetchTimeout>"),
                           build=SimpleNamespace(timeout="<timeoutSeconds>"))
    build = supervisor.phase_unit_argv(spec, "build", supervisor.DIRECTORY, "<uid>")
    launcher = supervisor.launcher_argv(spec, supervisor.DIRECTORY, "<uid>")
    assert blocks == [" ".join(build), " ".join(launcher)]
    fetch = supervisor.phase_unit_argv(spec, "fetch", supervisor.DIRECTORY, "<uid>")
    differences = [(ours, theirs) for ours, theirs in zip(fetch, build) if ours != theirs]
    assert len(fetch) == len(build) and differences == [
        (f"--unit={supervisor.UNITS['fetch']}", f"--unit={supervisor.UNITS['build']}"),
        ("--property=RuntimeMaxSec=<fetchTimeout>", "--property=RuntimeMaxSec=<timeoutSeconds>"),
        ("--property=StandardInput=null", f"--property=StandardInput=file:{supervisor.DIRECTORY}/secrets"),
        ("fetch", "build")]
    assert f"`{supervisor.UNITS['fetch']}`, its own phase's `timeoutSeconds`, `StandardInput=null`, and " \
           "`phase fetch`" in UNWRAPPED
    started = " ".join(supervisor.supervisor_unit_argv(supervisor.DIRECTORY))
    assert started == ("systemd-run --unit=pb-lane-supervisor.service --slice=system.slice --service-type=exec "
                       "--expand-environment=no --quiet --property=TimeoutStopSec=30 -- /usr/bin/python3 "
                       "/run/projectbuddy-lane/supervisor.py build")
    assert f"`{' '.join(supervisor.kill_argv('<unit>'))}`" in UNWRAPPED
    assert "with one `--output` for each declared output" in UNWRAPPED


def test_the_guest_rules_are_documented():
    assert supervisor.STOP_TIMEOUT == 30 and "after `TimeoutStopSec`, 30 seconds" in UNWRAPPED
    assert supervisor.BACKSTOP_MARGIN == 60 and "`timeoutSeconds` plus 30 plus 60 seconds" in UNWRAPPED
    assert supervisor.EMPTY_TIMEOUT == 30 and "`populated 0` or the group is gone, for up to 30 seconds" in UNWRAPPED
    assert supervisor.CARRIER_TIMEOUT == 300 and "waits up to 300 seconds for that" in UNWRAPPED
    assert supervisor.NOT_FOUND == 127 and "A step it cannot find exits 127" in UNWRAPPED
    assert supervisor.NOT_EXECUTABLE == 126 and "other reason exits 126" in UNWRAPPED
    assert supervisor.UNIT_RUNNING == ("active", "deactivating")
    assert "other state than `active` or `deactivating` has lost its result" in UNWRAPPED
    assert f"`{supervisor.SLICE}/<unit>/cgroup.events`" in UNWRAPPED.replace("/sys/fs/cgroup/", "")
    assert "only entry in `/sys/class/net` that has a `device` link" in UNWRAPPED
    assert f"`{supervisor.DIRECTORY}/{supervisor.SECRETS_NAME}`" in UNWRAPPED
    assert "`NAME=VALUE` followed by a NUL byte for each declared secret, in declared order" in UNWRAPPED
    assert "mode 0755" in UNWRAPPED and "with mode 0644" in UNWRAPPED
    uuid = "aabbccdd-eeff-0011-2233-445566778899"
    assert f"`{plan.OUTPUT_DEVICE_PREFIX}`" in UNWRAPPED
    assert f"medium UUID `{uuid}` it is `{plan.output_device(uuid)}`" in UNWRAPPED
    assert ("apart from the [SSH channel](#the-ssh-channel), the [guest supervisor](#guest-phases), and the "
            "output disk's launcher and reader") in UNWRAPPED


def test_the_client_key_property_is_documented():
    assert f"`{channel.ACCESS_PROPERTY}` with the flag `RDONLYGUEST`" in UNWRAPPED
    line = next(line for line in plan.VAGRANTFILE.splitlines() if "guestproperty" in line)
    assert '"--flags", "RDONLYGUEST"]' in line and "{access_property}" in line and "File.read({public_key})" in line
    assert f"read from `{channel.PUBLIC_KEY}` in the run directory" in UNWRAPPED
    assert f"`{channel.CLIENT_KEY}` and `{channel.PUBLIC_KEY}`" in UNWRAPPED


def test_the_ssh_channel_is_the_code(tmp_path):
    [block] = code_blocks("### The SSH channel", "### Guest phases")
    session = channel.Session(channel.Guest(Path("/RUNDIR"), 2222, "SHA256:" + "A" * 43))
    argv = " ".join(session.argv).replace("/RUNDIR", "<run directory>").replace(" 2222 ", " <port> ")
    assert block == f"{argv} <command>"
    calls = []
    run_dir = tmp_path / "run"
    run_dir.mkdir(mode=0o700)
    try:
        channel.generate_client_key(run_dir, "pb-lane-0123456789ab", lambda argv, **kwargs: calls.append(argv) or (1, ""))
    except channel.LaneBoxError:
        pass
    [keygen] = calls
    words = ['""' if word == "" else word for word in keygen]
    shown = " ".join(words).replace("pb-lane-0123456789ab", "<instance name>").replace(str(run_dir), "<run directory>")
    assert f"`{shown}`" in UNWRAPPED
    assert channel.SSH_PATH.pattern == "/[A-Za-z0-9._/@+-]*"
    assert UNWRAPPED.count(f"`{channel.SSH_PATH.pattern}`") == 2
    assert channel.KEYGEN_TIMEOUT == 60 and "for up to 60 seconds, and accepts the result" in UNWRAPPED
    assert (clone_guest.ENROLL_ATTEMPTS, clone_guest.ENROLL_INTERVAL, clone_guest.ENROLL_TIMEOUT) == (30, 5, 20)
    assert "up to 30 times, 5 seconds apart, for up to 20 seconds each" in UNWRAPPED
    assert (channel.AUTH_ATTEMPTS, channel.AUTH_INTERVAL, channel.SSH_FAILED) == (12, 5, 255)
    assert "retrying up to 12 times, 5 seconds apart, while ssh itself fails with exit code 255" in UNWRAPPED
    assert clone_guest.QUERY_TIMEOUT == 60 and "`check-runner-units`' queries time out after 60 seconds each" in UNWRAPPED
    assert f"`[{clone_guest.GUEST_HOST}]:<port> ssh-ed25519 <key>`" in UNWRAPPED
    assert f"refuses a run directory that already has a `{channel.KNOWN_HOSTS}` file" in UNWRAPPED
    assert channel.ACCOUNT == "root" and "runs `id -u` as root" in UNWRAPPED
