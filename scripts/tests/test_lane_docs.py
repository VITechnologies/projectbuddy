"""Ubuntu lane: the procedure stays in step with the teardown code."""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lane import cli, common, teardown  # noqa: E402

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
