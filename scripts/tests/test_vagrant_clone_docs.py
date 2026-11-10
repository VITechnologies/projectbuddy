"""Vagrant clone check: the procedure stays in step with the code."""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lane_box.readiness import RUNNER_UNIT  # noqa: E402
from vagrant_clone import core, guest, live, profile, receipt  # noqa: E402

REPOSITORY = Path(__file__).resolve().parents[2]
DOCS = REPOSITORY / "docs"
PROCEDURE = (DOCS / "vm-provisioning" / "ubuntu-vagrant-clone.md").read_text(encoding="utf-8")
UNWRAPPED = " ".join(PROCEDURE.split())
APPLIANCE = REPOSITORY / "vm" / "ubuntu-labview"
ADAPTERS = (APPLIANCE / "verify-vagrant-clone.py", APPLIANCE / "verify-vagrant-clone-receipt.py")


def table_rows(heading: str) -> list[list[str]]:
    """The cells of each body row of the table that follows ``heading``."""
    section = PROCEDURE.split(heading, 1)[1].split("\n\n|", 1)[1].split("\n\n", 1)[0]
    rows = [line for line in ("|" + section).splitlines() if line.startswith("| ") and "---" not in line][1:]
    assert rows, heading
    return [[cell.strip() for cell in row.strip().strip("|").split("|")] for row in rows]


def table_names(heading: str) -> list[str]:
    """Backticked names in the first column of the table that follows ``heading``."""
    return [name for row in table_rows(heading) for name in re.findall(r"`([^`]+)`", row[0])]


def test_profile_fields_and_tests_are_documented():
    assert table_names("exactly these fields, plus the optional") == list(profile.FIELDS)
    assert profile.OPTIONAL_FIELDS == ("receiptV1Runtime",)
    assert "plus the optional `receiptV1Runtime`" in UNWRAPPED
    assert table_names("exactly one test:") == list(profile.TESTS)
    items = next(row for row in table_rows("exactly one test:") if row[0] == "`items`")
    assert [*profile.ITEMS_FIELDS, *profile.ITEMS_OPTIONAL] == ["count", "where", "each"]
    assert all(f"`{field}`" in items[1] for field in ("count", "where", "each"))
    assert all(f"`{field}`" in PROCEDURE for field in profile.CAPTURE_FIELDS + profile.BOOT_ID_FIELDS)
    assert all(f"`{policy}`" in PROCEDURE for policy in profile.RUNNER_POLICIES)


def test_profile_limits_are_documented():
    assert f"`{profile.PROFILE_SCHEMA}`" in PROCEDURE
    assert f"`timeoutSeconds` (1 to {profile.MAX_TIMEOUT})" in UNWRAPPED
    assert f"matching `{profile.NAME_PATTERN.pattern}`" in PROCEDURE


def test_runner_unit_queries_and_pattern_are_documented():
    bullets = re.findall(r"^- `(systemctl [^`]+)`$", PROCEDURE.split("## Runner units", 1)[1], re.MULTILINE)
    assert bullets == [" ".join(query) for query in core.RUNNER_UNIT_QUERIES]
    assert f"`{RUNNER_UNIT.pattern}`" in PROCEDURE


def test_receipt_fields_are_documented(tmp_path):
    loaded = profile.load_profile(APPLIANCE / "clone-profile.json")
    for name in loaded.files:
        (tmp_path / name).write_text("{}", encoding="utf-8")
    value = receipt.build_receipt(loaded, box={"receiptSha256": "0", "boxSha256": "1"}, name="n", uuid="u",
                                  boot="b", evidence_dir=tmp_path, observed=[])
    assert value["schema"] == receipt.RECEIPT_SCHEMA
    assert f"`{receipt.RECEIPT_SCHEMA}` receipt" in PROCEDURE
    assert table_names(f"`{receipt.RECEIPT_SCHEMA}` receipt:") == [key for key in value if key != "schema"]
    rows = {re.findall(r"`([^`]+)`", row[0])[0]: row[1] for row in table_rows(f"`{receipt.RECEIPT_SCHEMA}` receipt:")}
    for block in ("profile", "sourceBox", "runnerUnits"):
        assert sorted(re.findall(r"`([^`]+)`", rows[block])) == sorted(value[block]), block
    assert re.findall(r"`([^`]+)`", rows["clone"]) == ["vm", "name", "uuid", "bootId", "finalState",
                                                        receipt.FINAL_STATE]
    assert sorted(value["clone"]) == ["bootId", "finalState", "vm"] and sorted(value["clone"]["vm"]) == ["name", "uuid"]
    assert rows["failures"] == "`[]`" and rows["outcome"] == f"`{value['outcome']}`"


def test_schemas_paths_and_timing_are_documented():
    assert f"`{receipt.RECEIPT_V1_SCHEMA}` receipts" in PROCEDURE
    assert f"`{receipt.RECEIPT_FILENAME}`" in PROCEDURE and f"`{receipt.RECEIPT_FILENAME}.sha256`" in PROCEDURE
    assert f"`{live.SCRATCH}`" in PROCEDURE
    assert f"`{live.TEMPORARY_PREFIX}*`" in PROCEDURE
    assert f"trying {guest.ENROLL_ATTEMPTS} times, {guest.ENROLL_INTERVAL} seconds apart" in UNWRAPPED
    assert f"after {guest.POWEROFF_POLLS} state checks, {guest.POWEROFF_INTERVAL} seconds apart" in UNWRAPPED
    assert "`V1_PROFILE_SHA256`" in PROCEDURE
    receipt_adapter = ADAPTERS[1].read_text(encoding="utf-8")
    assert "V1_PROFILE_SHA256 = " in receipt_adapter
    assert 'V1_PROFILE = SCRIPT_DIR / "clone-profile.v1.json"' in receipt_adapter
    assert "the frozen `clone-profile.v1.json`" in UNWRAPPED
    assert "`--profile` does not apply to `@1` receipts" in UNWRAPPED
    assert "The verifier reads both profiles for every receipt" in UNWRAPPED


def test_the_appliance_runner_unit_policy_is_documented():
    assert profile.load_profile(APPLIANCE / "clone-profile.json").runner_units == "refuse"
    assert "The appliance profile refuses runner units" in UNWRAPPED
    assert "Package the box before registering a runner on the source VM." in UNWRAPPED
    vm_procedure = " ".join((DOCS / "vm-provisioning" / "procedure.md").read_text(encoding="utf-8").split())
    assert "Package any Vagrant box before this phase" in vm_procedure
    assert "(ubuntu-vagrant-clone.md#the-appliance-profile)" in vm_procedure


def test_every_adapter_option_is_documented():
    for adapter in ADAPTERS:
        options = re.findall(r'add_argument\("(--[a-z0-9-]+)"', adapter.read_text(encoding="utf-8"))
        assert options, adapter.name
        missing = [option for option in options if not re.search(rf"{re.escape(option)}(?![a-z0-9-])", PROCEDURE)]
        assert missing == [], adapter.name


def test_exit_codes_are_documented():
    rows = table_rows("## Exit codes")
    codes = {re.findall(r"`(\d+)`", row[0])[0]: row[1:] for row in rows}
    assert sorted(codes, key=int) == ["0", "1", "2", "127"]
    assert sorted(code for code, cells in codes.items() if cells[1]) == ["0", "2"]
    clone_adapter = ADAPTERS[0].read_text(encoding="utf-8")
    assert sorted(re.findall(r"return fail\(.*, (\d+)\)$", clone_adapter, re.MULTILINE)) == ["1", "127"]
    assert "def fail(message: str, code: int = 2)" in clone_adapter
    assert re.findall(r"return (\d+)", ADAPTERS[1].read_text(encoding="utf-8")) == ["2", "0"]


def test_the_procedure_is_linked():
    lane = (DOCS / "vm-provisioning" / "ubuntu-lane.md").read_text(encoding="utf-8")
    row = next(line for line in lane.splitlines() if line.startswith("| `check-runner-units` |"))
    assert "(ubuntu-vagrant-clone.md#runner-units)" in row
    item_map = (DOCS / "information-item-map.md").read_text(encoding="utf-8")
    assert "| `docs/vm-provisioning/ubuntu-vagrant-clone.md` |" in item_map
    assert "`scripts/tests/test_vagrant_clone_docs.py`" in item_map
    assert "(ubuntu-vagrant-clone.md)" in (DOCS / "vm-provisioning" / "procedure.md").read_text(encoding="utf-8")
    assert "(docs/vm-provisioning/ubuntu-vagrant-clone.md)" in (REPOSITORY / "README.md").read_text(encoding="utf-8")
