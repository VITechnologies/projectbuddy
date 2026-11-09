"""Real, assertion-based unit tests for Assemble-WikiSpec.py's hub assembly
and fail-closed structure checks. Uses small synthetic wiki trees written to
tmp_path, never a clone of the real wiki.

Run with: pytest --cov=scripts scripts/tests/test_assemble_wiki_spec.py
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parent.parent / "Assemble-WikiSpec.py"
_spec = importlib.util.spec_from_file_location("assemble_wiki_spec", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
assemble_wiki_spec = importlib.util.module_from_spec(_spec)
sys.modules["assemble_wiki_spec"] = assemble_wiki_spec
_spec.loader.exec_module(assemble_wiki_spec)

SRS = """# SRS

Intro cites [[REQ-001]] and `REQ-002`.

<!-- table: | ID | Requirement | Verification | -->
- [[REQ-001]]
- [[REQ-002]]
<!-- /table -->

See [[Planned-Cases]].
"""
RTM = """# RTM

<!-- table: | ReqID | Requirement (short) | TestID | -->
- [[REQ-001]]
- [[REQ-002]]
<!-- /table -->
"""
ARCH = """# Arch

Preamble.

<!-- sections -->
- [[Context-View]]
- [[Planned-Cases]]
<!-- /sections -->
"""
REQ1 = """# REQ-001: First

## Requirement

The engine shall do one thing.

## Verification

Run [[TEST-001]].

## TestID

[[TEST-001]]
"""
REQ2 = """# REQ-002: Second, see [[REQ-001]]

## Requirement

The engine shall do another thing.

## Verification

Review.

## TestID

[[TEST-002]]
"""
CONTEXT = """# Context View

Context text.

<!-- sections -->
- [[Inner, Part: One]]
<!-- /sections -->
"""
INNER = "# Inner, Part: One\n\nInner text.\n"
CASES = """# Planned Cases

<!-- table: | Test | Stimulus | -->
- [[TEST-001]]
<!-- /table -->
"""
TEST1 = "# TEST-001\n\n## Stimulus\n\nPoke [[REQ-001]].\n"
TEST2 = "# TEST-002\n\n<!-- page-kind: stub -->\nNo definition yet.\n"

EXPECTED_SRS = """# SRS

Intro cites REQ-001 and `REQ-002`.

| ID | Requirement | Verification |
| --- | --- | --- |
| REQ-001 | The engine shall do one thing. | Run TEST-001. |
| REQ-002 | The engine shall do another thing. | Review. |

See [[Arch#planned-cases]].
"""
EXPECTED_RTM = """# RTM

| ReqID | Requirement (short) | TestID |
| --- | --- | --- |
| REQ-001 | First | TEST-001 |
| REQ-002 | Second, see REQ-001 | TEST-002 |
"""
EXPECTED_ARCH = """# Arch

Preamble.

## Context View

Context text.

### Inner, Part: One

Inner text.

## Planned Cases

| Test | Stimulus |
| --- | --- |
| TEST-001 | Poke REQ-001. |
"""


def write(root: Path, pages: dict[str, str]) -> Path:
    for rel, text in pages.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    return root


def split_wiki(root: Path, overrides: dict[str, str | None] | None = None) -> Path:
    pages = {
        "SRS.md": SRS,
        "RTM.md": RTM,
        "Arch.md": ARCH,
        "Home.md": "# Home\n\n<!-- page-kind: note -->\n- [[SRS]]\n",
        "req/REQ-001.md": REQ1,
        "req/REQ-002.md": REQ2,
        "arch/Context-View.md": CONTEXT,
        "arch/Inner, Part: One.md": INNER,
        "arch/Planned-Cases.md": CASES,
        "tests/TEST-001.md": TEST1,
        "tests/TEST-002.md": TEST2,
        ".obsidian/ignored.md": "# not a page [[Missing]]\n",
    }
    for rel, text in (overrides or {}).items():
        if text is None:
            pages.pop(rel)
        else:
            pages[rel] = text
    return write(root, pages)


def run(root: Path, tmp_path: Path, *extra: str) -> tuple[int, dict[str, str]]:
    out = tmp_path / "out"
    code = assemble_wiki_spec.main([
        "--wiki", str(root),
        "--emit", f"SRS={out / 'srs.md'}",
        "--emit", f"RTM={out / 'rtm.md'}",
        "--emit", f"Arch={out / 'overview.md'}",
        *extra,
    ])
    files = {p.name: p.read_bytes().decode("utf-8") for p in out.glob("*.md")} if out.exists() else {}
    return code, files


def errors(capsys) -> str:
    return capsys.readouterr().err


def test_split_layout_assembles_tables_sections_and_links(tmp_path, capsys):
    code, files = run(split_wiki(tmp_path / "wiki"), tmp_path, "--strict", "--max-page-bytes", "4096")
    assert code == 0, errors(capsys)
    assert files == {"srs.md": EXPECTED_SRS, "rtm.md": EXPECTED_RTM, "overview.md": EXPECTED_ARCH}
    assert "sha256=" in capsys.readouterr().out


def test_legacy_pages_are_copied_byte_for_byte(tmp_path, capsys):
    legacy = "# SRS\r\n\r\n| ID | Requirement |\r\n[[Arch#context-view]] REQ-001"
    root = write(tmp_path / "wiki", {"SRS.md": legacy, "RTM.md": "one line", "Arch.md": "# Arch\n"})
    code, files = run(root, tmp_path)
    assert code == 0, errors(capsys)
    assert files == {"srs.md": legacy, "rtm.md": "one line", "overview.md": "# Arch\n"}


def test_github_slug_matches_wiki_anchors():
    assert assemble_wiki_spec.github_slug("Evidence, Authority, And Cleanup") == "evidence-authority-and-cleanup"
    assert assemble_wiki_spec.github_slug("Interlock: Source Findings") == "interlock-source-findings"


def test_fenced_code_is_left_alone(tmp_path, capsys):
    page = CONTEXT.replace("Context text.", "```\n# shell comment [[REQ-001]]\n```")
    code, files = run(split_wiki(tmp_path / "wiki", {"arch/Context-View.md": page}), tmp_path, "--strict")
    assert code == 0, errors(capsys)
    assert "# shell comment [[REQ-001]]" in files["overview.md"]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"other/req-001.md": "# REQ-001\n"}, "duplicate page name"),
        ({"req/REQ-002.md": None}, "table lists missing ID page [[REQ-002]]"),
        ({"req/REQ-002.md": "# REQ-002\n\n<!-- page-kind: retired -->\nGone.\n"}, "table lists retired page [[REQ-002]]"),
        ({"req/REQ-001.md": REQ1.replace("# REQ-001: First\n", "# REQ-001: First\nstray\n")}, "only '## <Column>' sections"),
        ({"req/REQ-001.md": REQ1.replace("one thing.", "one\nthing.")}, "section 'Requirement' must be"),
        ({"req/REQ-001.md": REQ1.replace("one thing.", "one | thing.")}, "unescaped table pipe"),
        ({"req/REQ-001.md": REQ1 + "\n## Verification\n\nAgain.\n"}, "duplicate section 'Verification'"),
        ({"req/REQ-001.md": REQ1 + "\n## Notes\n\nExtra.\n"}, "not consumed by any table: ['Notes']"),
        ({"req/REQ-003.md": "# REQ-003\n\n## Requirement\n\nOrphan.\n"}, "in no table and has no page-kind"),
        ({"req/REQ-003.md": "# REQ-003\n\n"}, "no column sections and no page-kind marker"),
        ({"req/REQ-003.md": "# Not the ID\n"}, "first line must be '# REQ-003'"),
        ({"req/REQ-003.md": "# REQ-003\r\n"}, "ID pages must use LF"),
        ({"SRS.md": SRS.replace("- [[REQ-002]]\n", "- [[REQ-002]]\n- [[REQ-002]]\n")}, "listed twice"),
        ({"RTM.md": RTM.replace("- [[REQ-002]]\n", "")}, "REQ-002.md: sections not consumed by any table: ['TestID']"),
        ({"req/REQ-002.md": REQ2.replace("## TestID\n\n[[TEST-002]]\n", "").rstrip("\n").rsplit("\n\n", 0)[0] + "\n"}, "missing 'TestID' required by RTM"),
        ({"arch/Planned-Cases.md": CASES + "\n## Hidden\n\nText.\n"}, "may not contain headings"),
        ({"arch/Planned-Cases.md": CASES.replace("<!-- /table -->", "<!-- /table --> trailing")}, "malformed table or sections marker"),
        ({"Arch.md": ARCH.replace("- [[Planned-Cases]]\n", "- [[Planned-Cases]]\n- [[Nowhere]]\n")}, "missing page [[Nowhere]]"),
        ({"Arch.md": ARCH.replace("- [[Planned-Cases]]\n", "- [[Planned-Cases]]\n- [[TEST-001]]\n")}, "lists ID page [[TEST-001]]"),
        ({"Other.md": "# Other\n\n<!-- sections -->\n- [[Planned-Cases]]\n<!-- /sections -->\n"}, "listed by both"),
        ({"arch/Planned-Cases.md": "Planned Cases\n"}, "must start with '# <heading>'"),
        ({"arch/Planned-Cases.md": CASES.replace("\n", "\r\n")}, "split pages must use LF"),
        ({"arch/Orphan.md": "# Orphan\n\nGoverned-looking text.\n"}, "arch/Orphan.md: page is in no hub index"),
        ({"Arch.md": ARCH.replace("- [[Planned-Cases]]\n", "")}, "hub [[Planned-Cases]] holds governed content but is not emitted"),
        ({"Arch.md": ARCH.replace("- [[Context-View]]\n", ""), "arch/Context-View.md": "# Context View\n\nText.\n"}, "arch/Context-View.md: page is in no hub index"),
        ({"req/REQ-001.md": REQ1.replace("# REQ-001: First", "# REQ-001: First | extra")}, "title contains an unescaped table pipe"),
        ({"req/REQ-001.md": REQ1.replace("# REQ-001: First", "# REQ-001:  First")}, "title must be one nonblank line"),
        ({"req/REQ-001.md": REQ1.replace("## Requirement\n\n", "## Requirement\n")}, "section 'Requirement' must be one blank line, one nonblank line"),
        ({"req/REQ-001.md": REQ1 + "\n<!-- page-kind: stub -->\n"}, "marker must appear once, directly after the title"),
        ({"tests/TEST-002.md": TEST2 + "\n## Stimulus\n\nHidden governed text.\n"}, "stub pages may not hold column sections"),
        ({"tests/TEST-002.md": TEST2.replace("stub", "note")}, "page-kind 'note' is not allowed here"),
        ({"Home.md": "# Home\n\n<!-- page-kind: stub -->\n"}, "page-kind 'stub' is not allowed here"),
        ({"Home.md": "# Home\n\n<!-- page-kind: note -->\n<!-- sections -->\n"}, "may not hold table or sections markers"),
        ({"Arch.md": ARCH.replace("- [[Planned-Cases]]\n", "- [[Planned-Cases]]\n- [[Home]]\n")}, "section index lists note page [[Home]]"),
        ({"Extra.md": "# Extra\n\n<!-- sections -->\n- [[Lonely]]\n<!-- /sections -->\n", "Lonely.md": "# Lonely\n\nText.\n"}, "hub [[Extra]] holds governed content but is not emitted"),
    ],
)
def test_structural_errors_fail_closed(tmp_path, capsys, overrides, message):
    code, files = run(split_wiki(tmp_path / "wiki", overrides), tmp_path)
    assert code == 1
    assert files == {}
    assert message in errors(capsys)


def test_section_cycle_is_rejected(tmp_path, capsys):
    root = write(tmp_path / "wiki", {
        "A.md": "# A\n\n<!-- sections -->\n- [[B]]\n<!-- /sections -->\n",
        "B.md": "# B\n\n<!-- sections -->\n- [[A]]\n<!-- /sections -->\n",
    })
    assert assemble_wiki_spec.main(["--wiki", str(root)]) == 1
    assert "section cycle" in errors(capsys)


def test_depth_limit_is_enforced(tmp_path, capsys):
    pages = {}
    for level in range(7):
        kid = f"\n<!-- sections -->\n- [[P{level + 1}]]\n<!-- /sections -->\n" if level < 6 else ""
        pages[f"P{level}.md"] = f"# P{level}\n\nText.\n{kid}"
    root = write(tmp_path / "wiki", pages)
    assert assemble_wiki_spec.main(["--wiki", str(root)]) == 1
    assert "section depth exceeds h6" in errors(capsys)


@pytest.mark.parametrize(
    ("home", "message"),
    [
        ("# Home\n\n<!-- page-kind: note -->\n[[SRS|alias]]\n", "piped link"),
        ("# Home\n\n<!-- page-kind: note -->\n[[Arch#context-view]]\n", "heading-anchor link"),
        ("# Home\n\n<!-- page-kind: note -->\n[[Missing]]\n", "unresolved link [[Missing]]"),
        ("# Home\n\n<!-- page-kind: note -->\nPlain REQ-001 mention.\n", "unlinked mention of REQ-001"),
    ],
)
def test_strict_link_hygiene(tmp_path, capsys, home, message):
    code, _ = run(split_wiki(tmp_path / "wiki", {"Home.md": home}), tmp_path, "--strict")
    assert code == 1
    assert message in errors(capsys)


def test_strict_ignores_code_and_unknown_ids(tmp_path, capsys):
    home = "# Home\n\n<!-- page-kind: note -->\n`[[Missing]]` and `REQ-001` and REQ-999.\n"
    code, _ = run(split_wiki(tmp_path / "wiki", {"Home.md": home}), tmp_path, "--strict")
    assert code == 0, errors(capsys)


def test_page_budget(tmp_path, capsys):
    code, _ = run(split_wiki(tmp_path / "wiki"), tmp_path, "--strict", "--max-page-bytes", "100")
    assert code == 1
    assert "exceeds the 100-byte page budget" in errors(capsys)


def test_emit_rejects_non_hub_pages(tmp_path, capsys):
    root = split_wiki(tmp_path / "wiki")
    for name in ("REQ-001", "Context-View", "Missing"):
        assert assemble_wiki_spec.main(["--wiki", str(root), "--emit", f"{name}={tmp_path / 'x.md'}"]) == 1
        assert f"--emit {name}: not a hub page" in errors(capsys)


def test_emit_requires_page_equals_path(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        assemble_wiki_spec.main(["--wiki", str(tmp_path), "--emit", "SRS"])
    assert exc.value.code == 2
    assert "--emit expects PAGE=PATH" in errors(capsys)


def test_emit_rejects_duplicate_output_paths(tmp_path, capsys):
    root = split_wiki(tmp_path / "wiki")
    target = tmp_path / "same.md"
    args = ["--wiki", str(root), "--emit", f"SRS={target}", "--emit", f"RTM={target}", "--emit", f"Arch={tmp_path / 'a.md'}"]
    assert assemble_wiki_spec.main(args) == 1
    assert "is used twice" in errors(capsys)
    assert not target.exists()


def test_summary_counts_stub_citations(tmp_path, capsys):
    code, _ = run(split_wiki(tmp_path / "wiki"), tmp_path)
    assert code == 0, errors(capsys)
    out = capsys.readouterr().out
    assert "'stub': 1" in out and "'note': 1" in out and "'hub': 3" in out
    assert "1 governed ID pages cite stub tests" in out
    assert not list((tmp_path / "out").glob(".*.assembling"))


def test_hubs_can_be_assembled_in_place(tmp_path, capsys):
    root = split_wiki(tmp_path / "wiki")
    args = ["--wiki", str(root)]
    for name in ("SRS", "RTM", "Arch"):
        args += ["--emit", f"{name}={root / (name + '.md')}"]
    assert assemble_wiki_spec.main(args) == 0, errors(capsys)
    assert (root / "SRS.md").read_text(encoding="utf-8") == EXPECTED_SRS
    assert (root / "RTM.md").read_text(encoding="utf-8") == EXPECTED_RTM
    assert (root / "Arch.md").read_text(encoding="utf-8") == EXPECTED_ARCH
    assert not list(root.glob(".*.assembling"))


def test_unsplit_wiki_in_place_is_unchanged(tmp_path, capsys):
    pages = {"Home.md": "# Home\r\n\r\nSee [[SRS#intro]].", "SRS.md": "# SRS\n\n| ID | Req |\n| --- | --- |\n| REQ-001 | One. |\n"}
    root = write(tmp_path / "wiki", pages)
    assert assemble_wiki_spec.main(["--wiki", str(root), "--emit", f"SRS={root / 'SRS.md'}"]) == 0, errors(capsys)
    assert (root / "SRS.md").read_bytes() == pages["SRS.md"].encode("utf-8")
    assert sorted(p.name for p in root.iterdir()) == ["Home.md", "SRS.md"]
