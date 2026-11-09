"""Approval gate: the adopter procedure and example registry stay in step with the code."""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from approval_gate import preflight, readiness, registry, verify  # noqa: E402

DOCS = Path(__file__).resolve().parents[2] / "docs" / "approval-gate"
PROCEDURE = (DOCS / "procedure.md").read_text(encoding="utf-8")


def table_codes(heading: str) -> "list[str]":
    """First-column codes of the table that follows `heading`."""
    section = PROCEDURE.split(heading, 1)[1]
    rows = re.findall(r"^\| `([a-z_-]+)` \|", section.split("\n#", 1)[0], flags=re.MULTILINE)
    assert rows, heading
    return rows


def test_the_example_registry_is_the_builtin_noop_class():
    assert registry.load_registry(DOCS / "classes.example.json") == registry.default_registry()


def test_every_readiness_finding_is_documented_with_its_fix():
    assert sorted(table_codes("Each finding carries a code and its fix:")) == sorted(readiness.FIXES)


def test_every_refusal_reason_is_documented_in_order():
    assert table_codes("### Refusal reasons") == list(verify.REASONS)


def test_every_registry_key_is_documented():
    assert sorted(table_codes("### 2. Declare the action classes")) == sorted(registry.CLASS_KEYS)


def test_the_documented_staged_scan_limits_match_the_gate():
    unwrapped = " ".join(PROCEDURE.split())
    assert preflight.SCAN_BYTES % (1024 * 1024) == 0
    assert (f"more than {preflight.LISTING_LIMIT} files, more than {preflight.SCAN_BYTES // (1024 * 1024)} MiB"
            in unwrapped)
