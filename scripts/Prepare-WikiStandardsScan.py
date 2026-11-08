#!/usr/bin/env python3
"""Validate and stage the authoritative wiki specifications for an assurance scan."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


DOCUMENTS = (
    ("System-Requirements-Specification.md", "docs/requirements/syrs.md"),
    ("Software-Requirements-Specification.md", "docs/requirements/srs.md"),
    ("Requirements-Traceability-Matrix.md", "docs/requirements/rtm.md"),
    ("Architecture-Packet.md", "docs/architecture/overview.md"),
)
SOFTWARE_REF = re.compile(r"(?<![A-Z0-9-])REQ-[A-Za-z0-9-]+")
TEST_REF = re.compile(r"(?<![A-Z0-9-])TEST-\d{3,}")
# Match the pinned checker's row parser so no requirement is silently skipped.
QUALITY_ROW = re.compile(r"^\|\s([A-Z0-9-]*REQ-\d+)\s\|\s(.*?)\s\|")


class SpecError(ValueError):
    """A required wiki input or its traceability is invalid."""


def _read_documents(wiki: Path) -> dict[str, bytes]:
    documents: dict[str, bytes] = {}
    for name, _ in DOCUMENTS:
        path = wiki / name
        if path.is_symlink() or not path.is_file():
            raise SpecError(f"missing or non-regular wiki document: {name}")
        data = path.read_bytes()
        if not data.strip():
            raise SpecError(f"empty wiki document: {name}")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SpecError(f"non-UTF-8 wiki document: {name}") from exc
        documents[name] = data
    return documents


def _rows(
    text: str, prefix: str, columns: int, label: str, *, checked_for_quality: bool = False
) -> dict[str, list[str]]:
    rows: dict[str, list[str]] = {}
    prefix_pattern = re.compile(rf"^\|\s*{re.escape(prefix)}")
    id_pattern = re.compile(rf"{re.escape(prefix)}\d{{3,}}")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not prefix_pattern.match(line):
            continue
        cells = [cell.strip() for cell in line.split("|")]
        if len(cells) != columns + 2 or cells[0] or cells[-1] or not all(cells[1:-1]):
            raise SpecError(f"malformed or incomplete requirement row in {label}")
        identifier = cells[1]
        if not id_pattern.fullmatch(identifier):
            raise SpecError(f"malformed requirement ID in {label}: {identifier}")
        if checked_for_quality:
            match = QUALITY_ROW.match(raw_line)
            if not match or match.group(1) != identifier:
                raise SpecError(f"requirement row is invisible to pinned quality checker: {identifier}")
        if identifier in rows:
            raise SpecError(f"duplicate requirement ID in {label}: {identifier}")
        rows[identifier] = cells
    if not rows:
        raise SpecError(f"no {prefix} requirement rows in {label}")
    return rows


def _require_same_ids(spec: dict[str, list[str]], rtm: dict[str, list[str]], label: str) -> None:
    missing = sorted(spec.keys() - rtm.keys())
    extra = sorted(rtm.keys() - spec.keys())
    if missing or extra:
        raise SpecError(f"{label} RTM IDs differ from specification: missing={missing}, extra={extra}")


def validate_documents(documents: dict[str, bytes]) -> None:
    if documents["System-Requirements-Specification.md"] == documents["Software-Requirements-Specification.md"]:
        raise SpecError("SyRS and SRS must be distinct documents")
    system = _rows(
        documents["System-Requirements-Specification.md"].decode("utf-8"),
        "SYS-REQ-",
        5,
        "SyRS",
        checked_for_quality=True,
    )
    software = _rows(
        documents["Software-Requirements-Specification.md"].decode("utf-8"),
        "REQ-",
        5,
        "SRS",
        checked_for_quality=True,
    )
    for label, requirements in (("SyRS", system), ("SRS", software)):
        for identifier, cells in requirements.items():
            if not re.search(r"\bshall\b", cells[2], re.IGNORECASE):
                raise SpecError(f"{label} requirement lacks normative shall: {identifier}")
    rtm_text = documents["Requirements-Traceability-Matrix.md"].decode("utf-8")
    system_rtm = _rows(rtm_text, "SYS-REQ-", 8, "system RTM")
    software_rtm = _rows(rtm_text, "REQ-", 8, "software RTM")
    _require_same_ids(system, system_rtm, "system")
    _require_same_ids(software, software_rtm, "software")

    software_tests: dict[str, set[str]] = {}
    for identifier, cells in software_rtm.items():
        tests = set(TEST_REF.findall(cells[5]))
        if not tests:
            raise SpecError(f"software RTM row has no test ID: {identifier}")
        software_tests[identifier] = tests

    allocated: set[str] = set()
    for identifier, cells in system_rtm.items():
        refs = set(SOFTWARE_REF.findall(cells[7]))
        if not refs:
            raise SpecError(f"system requirement has no software allocation: {identifier}")
        malformed = sorted(ref for ref in refs if not re.fullmatch(r"REQ-\d{3,}", ref))
        unknown = sorted(ref for ref in refs if ref not in software)
        if malformed or unknown:
            raise SpecError(f"invalid software allocation for {identifier}: {sorted(refs)}")
        tests = set(TEST_REF.findall(cells[5]))
        allocated_tests = set().union(*(software_tests[ref] for ref in refs))
        if not tests or not tests <= allocated_tests:
            raise SpecError(f"invalid test allocation for {identifier}: {sorted(tests)}")
        allocated.update(refs)

    unallocated = sorted(software.keys() - allocated)
    if unallocated:
        raise SpecError(f"software requirements without system allocation: {unallocated}")


def prepare(wiki: Path, checkout: Path) -> None:
    if not checkout.is_dir():
        raise SpecError("scan checkout is not a directory")
    documents = _read_documents(wiki)
    validate_documents(documents)

    destinations = [(checkout / destination, documents[name]) for name, destination in DOCUMENTS]
    for path, _ in destinations:
        if path.exists() or path.is_symlink():
            raise SpecError(f"scan destination already exists: {path.relative_to(checkout)}")
    for path, data in destinations:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as handle:
            handle.write(data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wiki", type=Path, help="authoritative wiki checkout")
    parser.add_argument("checkout", type=Path, help="ephemeral scan checkout")
    args = parser.parse_args(argv)
    try:
        prepare(args.wiki, args.checkout)
    except (SpecError, OSError) as exc:
        print(f"standards input validation failed: {exc}", file=sys.stderr)
        return 1
    print("Validated and staged distinct SyRS, SRS, RTM, and architecture inputs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
