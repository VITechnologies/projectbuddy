"""Ubuntu lane: the procedure stays in step with the teardown code."""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lane import cli, common, declaration, plan, teardown  # noqa: E402
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
    assert plan.LINK_OFF_PHASES == ("build",) and "its link off during the `build` phase" in UNWRAPPED
    assert f"`{plan.UUID_PLACEHOLDER}` in an argv" in UNWRAPPED


def test_the_step_table_is_the_code():
    request = SimpleNamespace(work_dir=Path("/work"), box=Path("/box"), engine=Path("/engine"),
                              records_dir=Path("/records"))
    tool = {"name": "<tool>", "probe": {"argv": ["tool", "--version"]}}
    workload = SimpleNamespace(toolchain=(tool,), argv=("run",), timeout=1, secrets=(), working_directory=".")
    steps = plan.plan_steps(request, SimpleNamespace(vboxmanage="VBoxManage", vagrant="vagrant"), workload,
                            "pb-lane-0123456789ab", "0" * 64)
    rows = table("| Step | Phase | Runs | What it does |")
    assert [(names(row[0])[0], names(row[1])[0], row[2]) for row in rows] == [
        (step["name"], step["phase"], step["runs"]) for step in steps]
    assert {step["phase"] for step in steps} == set(plan.PHASES) - {"fetch"}
    documented = {names(row[0])[0]: row[3] for row in rows}
    for step in steps:
        if step["runs"] == "host" and step["argv"][0] == "VBoxManage" and step["name"] != "verify-network-off":
            assert f"`{' '.join(step['argv'])}`" in documented[step["name"]], step["name"]


def test_plan_timeouts_are_documented():
    assert PROBE_TIMEOUT == 60 and "for up to 60 seconds" in UNWRAPPED
    assert plan.HOST_DEADLINE_GRACE == 300 and "its timeout plus 300 seconds" in UNWRAPPED
    assert plan.QUERY_TIMEOUT == 120 and plan.GIT_TIMEOUT == 120
    assert "Host queries time out after 120 seconds, `git` after 120 seconds" in UNWRAPPED
    assert plan.BOX_ADD_TIMEOUT == plan.UP_TIMEOUT == 1800
    assert "`box-add` and `up` after 1800 seconds each" in UNWRAPPED
