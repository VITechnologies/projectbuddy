"""Offline tests for authoritative wiki staging and allocation checks."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


_MODULE_PATH = Path(__file__).resolve().parent.parent / "Prepare-WikiStandardsScan.py"
_spec = importlib.util.spec_from_file_location("prepare_wiki_standards_scan", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
scan = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scan)

SYSTEM = "System-Requirements-Specification.md"
SOFTWARE = "Software-Requirements-Specification.md"
RTM = "Requirements-Traceability-Matrix.md"
ARCHITECTURE = "Architecture-Packet.md"

SYSTEM_ROW = "| SYS-REQ-001 | The system shall provide output. | Owner | High | Output exists. |"
SOFTWARE_ROWS = (
    "| REQ-001 | The component shall build. | Owner | High | Build exists. |\n"
    "| REQ-002 | The component shall report. | Owner | High | Report exists. |"
)
SYSTEM_RTM_ROW = (
    "| SYS-REQ-001 | System output | Owner | High | TEST-001 | SyRS | "
    "REQ-001, REQ-002 | Planned |"
)
SOFTWARE_RTM_ROWS = (
    "| REQ-001 | Build | Owner | High | TEST-001 | SRS | build | Planned |\n"
    "| REQ-002 | Report | Owner | High | TEST-002 | SRS | report | Planned |"
)


@pytest.fixture
def inputs(tmp_path: Path) -> tuple[Path, Path]:
    wiki = tmp_path / "wiki"
    checkout = tmp_path / "checkout"
    wiki.mkdir()
    checkout.mkdir()
    (wiki / SYSTEM).write_text(f"# System\n{SYSTEM_ROW}\n", encoding="utf-8")
    (wiki / SOFTWARE).write_text(f"# Software\n{SOFTWARE_ROWS}\n", encoding="utf-8")
    (wiki / RTM).write_text(f"# RTM\n{SOFTWARE_RTM_ROWS}\n{SYSTEM_RTM_ROW}\n", encoding="utf-8")
    (wiki / ARCHITECTURE).write_text("# Architecture\n", encoding="utf-8")
    return wiki, checkout


def replace(wiki: Path, name: str, old: str, new: str) -> None:
    path = wiki / name
    content = path.read_text(encoding="utf-8")
    assert old in content
    path.write_text(content.replace(old, new), encoding="utf-8")


def test_stages_validated_documents_without_changing_wiki(inputs: tuple[Path, Path]) -> None:
    wiki, checkout = inputs
    scan.prepare(wiki, checkout)
    for source, destination in scan.DOCUMENTS:
        assert (checkout / destination).read_bytes() == (wiki / source).read_bytes()
    assert (checkout / "docs/requirements/syrs.md").read_bytes() != (
        checkout / "docs/requirements/srs.md"
    ).read_bytes()


@pytest.mark.parametrize("name", [SYSTEM, SOFTWARE, RTM, ARCHITECTURE])
def test_missing_document_fails_before_staging(inputs: tuple[Path, Path], name: str) -> None:
    wiki, checkout = inputs
    (wiki / name).unlink()
    with pytest.raises(scan.SpecError, match="missing or non-regular"):
        scan.prepare(wiki, checkout)
    assert not (checkout / "docs").exists()


@pytest.mark.parametrize("name", [SYSTEM, SOFTWARE, RTM, ARCHITECTURE])
def test_empty_document_fails(inputs: tuple[Path, Path], name: str) -> None:
    wiki, checkout = inputs
    (wiki / name).write_text(" \n", encoding="utf-8")
    with pytest.raises(scan.SpecError, match="empty wiki document"):
        scan.prepare(wiki, checkout)


def test_non_utf8_document_fails(inputs: tuple[Path, Path]) -> None:
    wiki, checkout = inputs
    (wiki / SYSTEM).write_bytes(b"\xff")
    with pytest.raises(scan.SpecError, match="non-UTF-8"):
        scan.prepare(wiki, checkout)


def test_symlinked_document_fails(inputs: tuple[Path, Path]) -> None:
    wiki, checkout = inputs
    (wiki / SYSTEM).unlink()
    (wiki / SYSTEM).symlink_to(wiki / SOFTWARE)
    with pytest.raises(scan.SpecError, match="non-regular"):
        scan.prepare(wiki, checkout)


def test_identical_specifications_fail(inputs: tuple[Path, Path]) -> None:
    wiki, checkout = inputs
    (wiki / SOFTWARE).write_bytes((wiki / SYSTEM).read_bytes())
    with pytest.raises(scan.SpecError, match="distinct documents"):
        scan.prepare(wiki, checkout)


@pytest.mark.parametrize(
    ("name", "row"),
    [(SYSTEM, SYSTEM_ROW), (SOFTWARE, SOFTWARE_ROWS.splitlines()[0]), (RTM, SYSTEM_RTM_ROW)],
)
def test_duplicate_ids_fail(inputs: tuple[Path, Path], name: str, row: str) -> None:
    wiki, checkout = inputs
    replace(wiki, name, row, f"{row}\n{row}")
    with pytest.raises(scan.SpecError, match="duplicate requirement ID"):
        scan.prepare(wiki, checkout)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("SYS-REQ-001", "SYS-REQ-one", "malformed requirement ID"),
        (SYSTEM_ROW, SYSTEM_ROW.replace(" | High | ", " |  | "), "incomplete requirement row"),
        (SYSTEM_ROW, "No system requirement rows.", "no SYS-REQ- requirement rows"),
    ],
)
def test_invalid_specification_rows_fail(
    inputs: tuple[Path, Path], old: str, new: str, message: str
) -> None:
    wiki, checkout = inputs
    replace(wiki, SYSTEM, old, new)
    with pytest.raises(scan.SpecError, match=message):
        scan.prepare(wiki, checkout)


@pytest.mark.parametrize("name", [SYSTEM, SOFTWARE])
def test_non_normative_requirement_fails(inputs: tuple[Path, Path], name: str) -> None:
    wiki, checkout = inputs
    replace(wiki, name, " shall ", " will ")
    with pytest.raises(scan.SpecError, match="lacks normative shall"):
        scan.prepare(wiki, checkout)


@pytest.mark.parametrize(
    ("name", "old", "new"),
    [
        (SYSTEM, "| SYS-REQ-001 |", "|  SYS-REQ-001 |"),
        (SYSTEM, "SYS-REQ-001 |", "SYS-REQ-001  |"),
        (SYSTEM, SYSTEM_ROW, f"  {SYSTEM_ROW}"),
        (SOFTWARE, "| REQ-001 | The", "| REQ-001 |The"),
    ],
)
def test_rows_invisible_to_pinned_quality_checker_fail(
    inputs: tuple[Path, Path], name: str, old: str, new: str
) -> None:
    wiki, checkout = inputs
    replace(wiki, name, old, new)
    with pytest.raises(scan.SpecError, match="invisible to pinned quality checker"):
        scan.prepare(wiki, checkout)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("SYS-REQ-001", "SYS-REQ-999", "system RTM IDs differ"),
        ("REQ-002 | Report", "REQ-999 | Report", "software RTM IDs differ"),
        ("REQ-001, REQ-002", "none", "no software allocation"),
        ("REQ-001, REQ-002", "REQ-001, REQ-999", "invalid software allocation"),
        ("REQ-001, REQ-002", "REQ-001, REQ-bad", "invalid software allocation"),
        ("REQ-001, REQ-002", "REQ-001", "without system allocation"),
        ("TEST-001 | SyRS", "TEST-999 | SyRS", "invalid test allocation"),
        ("TEST-001 | SyRS", "none | SyRS", "invalid test allocation"),
        ("TEST-001 | SRS", "none | SRS", "software RTM row has no test ID"),
    ],
)
def test_invalid_traceability_fails(
    inputs: tuple[Path, Path], old: str, new: str, message: str
) -> None:
    wiki, checkout = inputs
    replace(wiki, RTM, old, new)
    with pytest.raises(scan.SpecError, match=message):
        scan.prepare(wiki, checkout)
    assert not (checkout / "docs").exists()


def test_system_test_must_belong_to_its_allocated_software(inputs: tuple[Path, Path]) -> None:
    wiki, checkout = inputs
    replace(wiki, SYSTEM, SYSTEM_ROW, f"{SYSTEM_ROW}\n{SYSTEM_ROW.replace('SYS-REQ-001', 'SYS-REQ-002')}")
    first = SYSTEM_RTM_ROW.replace("TEST-001", "TEST-002").replace("REQ-001, REQ-002", "REQ-001")
    second = SYSTEM_RTM_ROW.replace("SYS-REQ-001", "SYS-REQ-002").replace(
        "TEST-001 | SyRS | REQ-001, REQ-002", "TEST-002 | SyRS | REQ-002"
    )
    replace(wiki, RTM, SYSTEM_RTM_ROW, f"{first}\n{second}")
    with pytest.raises(scan.SpecError, match="invalid test allocation for SYS-REQ-001"):
        scan.prepare(wiki, checkout)


def test_existing_destination_is_never_overwritten(inputs: tuple[Path, Path]) -> None:
    wiki, checkout = inputs
    destination = checkout / "docs/requirements/srs.md"
    destination.parent.mkdir(parents=True)
    destination.write_text("existing", encoding="utf-8")
    with pytest.raises(scan.SpecError, match="destination already exists"):
        scan.prepare(wiki, checkout)
    assert destination.read_text(encoding="utf-8") == "existing"
    assert not (checkout / "docs/requirements/syrs.md").exists()


def test_main_reports_success_and_validation_errors(
    inputs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    wiki, checkout = inputs
    assert scan.main([str(wiki), str(checkout / "missing")]) == 1
    assert "checkout is not a directory" in capsys.readouterr().err
    assert scan.main([str(wiki), str(checkout)]) == 0
    assert "Validated and staged" in capsys.readouterr().out


def test_main_reports_io_errors(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def deny_access(_wiki: Path, _checkout: Path) -> None:
        raise PermissionError("access denied")

    monkeypatch.setattr(scan, "prepare", deny_access)
    assert scan.main(["wiki", "checkout"]) == 1
    assert "access denied" in capsys.readouterr().err


def test_workflow_checks_both_scopes_before_scoring() -> None:
    workflow = (_MODULE_PATH.parents[1] / ".github/workflows/standards-assurance-scan.yml").read_text(
        encoding="utf-8"
    )
    assemble = workflow.split("      - name: Assemble Scan Checkout", 1)[1].split(
        "      - name: Run Assurance Pipeline", 1
    )[0]
    assert "set -euo pipefail" in assemble
    assert assemble.index("Prepare-WikiStandardsScan.py") < assemble.index("requirements_quality_check.py")
    assert assemble.count("--requirements-spec-scope system --json") == 1
    assert assemble.count("--requirements-spec-scope software --json") == 1
    assert "checkout --quiet ba72187ce5f745a1ffea93536cf83982b747819a" in assemble
    assert "run_assurance.py" in workflow.split("      - name: Run Assurance Pipeline", 1)[1]
