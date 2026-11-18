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
        ({"req/REQ-001.md": REQ1.replace("one thing.", "one thing.\n")}, "section 'Requirement' must be nonblank text without edge whitespace"),
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
        ({"req/REQ-001.md": REQ1.replace("## Requirement\n\n", "## Requirement\n")}, "section 'Requirement' must be one blank line, its text, then a blank line"),
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


BASE = "https://example.test/wiki"
EXPECTED_RTM_VIEW = f"""# RTM

| ReqID | Requirement (short) | TestID |
| --- | --- | --- |
| [REQ-001]({BASE}/REQ-001) | First | [TEST-001]({BASE}/TEST-001) |
| [REQ-002]({BASE}/REQ-002) | Second, see [REQ-001]({BASE}/REQ-001) | [TEST-002]({BASE}/TEST-002) |
"""


def test_view_links_each_wikilink_to_its_wiki_page(tmp_path, capsys):
    views = tmp_path / "views"
    code, files = run(
        split_wiki(tmp_path / "wiki"), tmp_path, "--strict",
        "--view", f"RTM={views / 'rtm.md'}", "--view", f"SRS={views / 'srs.md'}", "--link-base", BASE + "/",
    )
    assert code == 0, errors(capsys)
    assert files == {"srs.md": EXPECTED_SRS, "rtm.md": EXPECTED_RTM, "overview.md": EXPECTED_ARCH}
    assert (views / "rtm.md").read_text(encoding="utf-8") == EXPECTED_RTM_VIEW
    srs = (views / "srs.md").read_text(encoding="utf-8")
    assert f"Intro cites [REQ-001]({BASE}/REQ-001) and `REQ-002`." in srs
    assert f"See [Planned-Cases]({BASE}/Planned-Cases)." in srs
    assert "[[" not in srs


def test_view_keeps_unresolvable_links_and_code(tmp_path, capsys):
    text = "# SRS\n\nSee [[SRS#intro]], [[Inner Page]], [[Missing]], [[A|B]] and `[[Inner Page]]`.\n"
    root = write(tmp_path / "wiki", {"SRS.md": text, "Inner Page.md": "# Inner\n"})
    view = tmp_path / "view.md"
    args = ["--wiki", str(root), "--emit", f"SRS={tmp_path / 'srs.md'}", "--view", f"SRS={view}", "--link-base", BASE]
    assert assemble_wiki_spec.main(args) == 0, errors(capsys)
    assert (tmp_path / "srs.md").read_text(encoding="utf-8") == text
    assert view.read_text(encoding="utf-8") == (
        f"# SRS\n\nSee [SRS#intro]({BASE}/SRS#intro), [Inner Page]({BASE}/Inner-Page), [[Missing]], [[A|B]] and `[[Inner Page]]`.\n"
    )


def test_view_requires_link_base(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        assemble_wiki_spec.main(["--wiki", str(tmp_path), "--view", f"SRS={tmp_path / 'v.md'}"])
    assert exc.value.code == 2
    assert "--view requires --link-base" in errors(capsys)
    with pytest.raises(SystemExit):
        assemble_wiki_spec.main(["--wiki", str(tmp_path), "--view", "SRS", "--link-base", BASE])
    assert "--view expects PAGE=PATH" in errors(capsys)


def test_view_is_checked_like_emit(tmp_path, capsys):
    out = tmp_path / "out"
    code, _ = run(split_wiki(tmp_path / "wiki"), tmp_path, "--view", f"REQ-001={tmp_path / 'v.md'}",
                  "--view", f"Arch={out / 'rtm.md'}", "--link-base", BASE)
    assert code == 1
    err = errors(capsys)
    assert "--view REQ-001: not a hub page" in err
    assert "--view Arch: output path" in err and "is used twice" in err
    assert not out.exists() and not (tmp_path / "v.md").exists()


STIMULUS = (
    "Member commit: projectbuddy `0123abc`. Run these steps in a clean checkout of it, on linux-x64:\n"
    "\n"
    "1. `python3 -m pytest scripts/tests/test_one.py`\n"
    "2. `python3 -m pytest scripts/tests/test_two.py`"
)
OBSERVATION = (
    "Each test case below passes at the member commit. Together they cover [[REQ-001]].\n"
    "\n"
    "- `scripts/tests/test_one.py` › test_first\n"
    "- `scripts/tests/test_two.py`"
)
CASES_ML = CASES.replace("| Test | Stimulus |", "| Test | Stimulus | Required observation |")
TEST1_ML = f"# TEST-001\n\n## Stimulus\n\n{STIMULUS}\n\n## Required observation\n\n{OBSERVATION}\n"
REQ1_ML = REQ1.replace("Run [[TEST-001]].", "Run the planned case:\n\n- [[TEST-001]] in [[Planned-Cases]]")
MULTI_LINE = {"arch/Planned-Cases.md": CASES_ML, "tests/TEST-001.md": TEST1_ML, "req/REQ-001.md": REQ1_ML}
ROW_ML = (
    "| TEST-001 | Member commit: projectbuddy `0123abc`. Run these steps in a clean checkout of it, on linux-x64:"
    "<br><br>1. `python3 -m pytest scripts/tests/test_one.py`<br>2. `python3 -m pytest scripts/tests/test_two.py` | "
    "Each test case below passes at the member commit. Together they cover REQ-001."
    "<br><br>- `scripts/tests/test_one.py` › test_first<br>- `scripts/tests/test_two.py` |"
)
EXPECTED_ARCH_ML = EXPECTED_ARCH.replace(
    "| Test | Stimulus |\n| --- | --- |\n| TEST-001 | Poke REQ-001. |\n",
    f"| Test | Stimulus | Required observation |\n| --- | --- | --- |\n{ROW_ML}\n",
)
EXPECTED_SRS_ML = EXPECTED_SRS.replace(
    "| Run TEST-001. |", "| Run the planned case:<br><br>- TEST-001 in [[Arch#planned-cases]] |"
)


def test_one_line_sections_assemble_as_before(tmp_path, capsys):
    # EXPECTED_* predate multi-line sections; one-line pages must still give exactly these bytes.
    code, files = run(split_wiki(tmp_path / "wiki"), tmp_path, "--strict", "--max-page-bytes", "4096")
    assert code == 0, errors(capsys)
    assert files == {"srs.md": EXPECTED_SRS, "rtm.md": EXPECTED_RTM, "overview.md": EXPECTED_ARCH}
    assert not any("<br>" in text.lower() for text in files.values())


def test_multi_line_sections_become_one_br_joined_cell(tmp_path, capsys):
    code, files = run(split_wiki(tmp_path / "wiki", MULTI_LINE), tmp_path, "--strict", "--max-page-bytes", "4096")
    assert code == 0, errors(capsys)
    assert files == {"srs.md": EXPECTED_SRS_ML, "rtm.md": EXPECTED_RTM, "overview.md": EXPECTED_ARCH_ML}
    rows = [line for line in files["overview.md"].split("\n") if line.startswith("| TEST-001 ")]
    assert rows == [ROW_ML]


def test_multi_line_cells_split_back_into_their_sections(tmp_path, capsys):
    code, files = run(split_wiki(tmp_path / "wiki", MULTI_LINE), tmp_path)
    assert code == 0, errors(capsys)
    row = next(line for line in files["overview.md"].split("\n") if line.startswith("| TEST-001 "))
    _, stimulus, observation = row[2:-2].split(" | ")
    assert stimulus.replace("<br>", "\n") == STIMULUS
    assert observation.replace("<br>", "\n") == OBSERVATION.replace("[[REQ-001]]", "REQ-001")


def test_assembled_multi_line_hubs_reassemble_unchanged(tmp_path, capsys):
    code, files = run(split_wiki(tmp_path / "wiki", MULTI_LINE), tmp_path)
    assert code == 0, errors(capsys)
    unsplit = write(tmp_path / "unsplit", {"SRS.md": files["srs.md"], "RTM.md": files["rtm.md"], "Arch.md": files["overview.md"]})
    args = ["--wiki", str(unsplit)]
    for name in ("SRS", "RTM", "Arch"):
        args += ["--emit", f"{name}={unsplit / (name + '.md')}"]
    assert assemble_wiki_spec.main(args) == 0, errors(capsys)
    assert (unsplit / "Arch.md").read_bytes() == files["overview.md"].encode("utf-8")
    assert (unsplit / "SRS.md").read_bytes() == files["srs.md"].encode("utf-8")


def test_view_rewrites_links_on_later_lines(tmp_path, capsys):
    views = tmp_path / "views"
    code, _ = run(
        split_wiki(tmp_path / "wiki", MULTI_LINE), tmp_path, "--strict",
        "--view", f"SRS={views / 'srs.md'}", "--view", f"Arch={views / 'arch.md'}", "--link-base", BASE,
    )
    assert code == 0, errors(capsys)
    srs = (views / "srs.md").read_text(encoding="utf-8")
    assert f"| Run the planned case:<br><br>- [TEST-001]({BASE}/TEST-001) in [Planned-Cases]({BASE}/Planned-Cases) |" in srs
    arch = (views / "arch.md").read_text(encoding="utf-8")
    assert f"Together they cover [REQ-001]({BASE}/REQ-001).<br><br>- `scripts/tests/test_one.py`" in arch


@pytest.mark.parametrize(
    ("later_line", "message"),
    [
        ("See [[Missing]].", "tests/TEST-001.md: unresolved link [[Missing]]"),
        ("See [[Arch#context-view]].", "tests/TEST-001.md: heading-anchor link [[Arch#context-view]]"),
        ("Also REQ-002.", "tests/TEST-001.md: unlinked mention of REQ-002"),
    ],
)
def test_strict_checks_links_on_later_lines(tmp_path, capsys, later_line, message):
    page = TEST1_ML.replace("- `scripts/tests/test_two.py`\n", f"- `scripts/tests/test_two.py`\n\n{later_line}\n")
    code, files = run(split_wiki(tmp_path / "wiki", {**MULTI_LINE, "tests/TEST-001.md": page}), tmp_path, "--strict")
    assert code == 1
    assert files == {}
    assert message in errors(capsys)


@pytest.mark.parametrize(
    ("stimulus", "message"),
    [
        ("Step one.<br>Step two.", "section 'Stimulus' contains a literal <br>; the hub table cell uses it"),
        ("Step one.\n\nStep <BR> two.", "section 'Stimulus' contains a literal <br> on line 3"),
        ("Step one.\nStep <Br> two.", "section 'Stimulus' contains a literal <br> on line 2"),
        ("\nStep one.\nStep two.", "section 'Stimulus' must be nonblank text without edge whitespace"),
        ("Step one.\nStep two.\n", "section 'Stimulus' must be nonblank text without edge whitespace"),
        (" Step one.\nStep two.", "section 'Stimulus' must be nonblank text without edge whitespace"),
        ("Step one.\nStep two. ", "section 'Stimulus' must be nonblank text without edge whitespace"),
        ("Step one.\n\n- `a` | `b`", "section 'Stimulus' contains an unescaped table pipe on line 3"),
        ("Step one.\n##\nStep two.", "section 'Stimulus' contains a '## ' heading line on line 2"),
        ("Step one.\n## \nStep two.", "section 'Stimulus' contains a '## ' heading line on line 2"),
        ("Step one.\n  ##\tStep two.", "section 'Stimulus' contains a '## ' heading line on line 2"),
    ],
)
def test_multi_line_sections_fail_closed(tmp_path, capsys, stimulus, message):
    page = TEST1_ML.replace(STIMULUS, stimulus)
    code, files = run(split_wiki(tmp_path / "wiki", {**MULTI_LINE, "tests/TEST-001.md": page}), tmp_path)
    assert code == 1
    assert files == {}
    assert f"tests/TEST-001.md: {message}" in errors(capsys)


def test_title_may_not_hold_a_cell_break(tmp_path, capsys):
    page = REQ1.replace("# REQ-001: First", "# REQ-001: First<br>Second")
    code, _ = run(split_wiki(tmp_path / "wiki", {"req/REQ-001.md": page}), tmp_path)
    assert code == 1
    assert "req/REQ-001.md: title contains a literal <br>; the hub table cell uses it" in errors(capsys)


def test_escaped_pipes_and_other_html_stay_allowed_on_later_lines(tmp_path, capsys):
    stimulus = "Step one.\n\n- `a` \\| `b`\n- <kbd>Enter</kbd> then ### stays text"
    page = TEST1_ML.replace(STIMULUS, stimulus)
    code, files = run(split_wiki(tmp_path / "wiki", {**MULTI_LINE, "tests/TEST-001.md": page}), tmp_path, "--strict")
    assert code == 0, errors(capsys)
    assert "| Step one.<br><br>- `a` \\| `b`<br>- <kbd>Enter</kbd> then ### stays text | " in files["overview.md"]


@pytest.mark.parametrize(
    "item",
    [
        "- `scripts/tests/test_two.py` › test_rules[allow-list-the allow-list differs from REQ-002]",
        "  * `scripts/tests/test_two.py` › Suite › REQ-002 case",
        "+ `scripts/tests/test_two.py` › REQ-002",
        "3. `scripts/tests/test_two.py` › test_rule[REQ-002]",
        "4) `scripts/tests/test_two.py` › test REQ-002",
    ],
)
def test_strict_accepts_ids_in_test_case_item_names(tmp_path, capsys, item):
    page = TEST1_ML.replace("- `scripts/tests/test_two.py`\n", f"{item}\n")
    code, files = run(split_wiki(tmp_path / "wiki", {**MULTI_LINE, "tests/TEST-001.md": page}), tmp_path, "--strict")
    assert code == 0, errors(capsys)
    assert f"<br>{item} |" in files["overview.md"]


@pytest.mark.parametrize(
    ("page", "line"),
    [
        ("tests/TEST-001.md", "Each test case below passes at the member commit. Together they cover REQ-002."),
        ("tests/TEST-001.md", "- scripts/tests/test_two.py › test_rules[differs from REQ-002]"),
        ("tests/TEST-001.md", "- `scripts/tests/test_two.py` test_rules[differs from REQ-002]"),
        ("tests/TEST-001.md", "`scripts/tests/test_two.py` › test_rules[differs from REQ-002]"),
        ("tests/TEST-001.md", "-`scripts/tests/test_two.py` › test_rules[differs from REQ-002]"),
        ("Home.md", "- `scripts/tests/test_two.py` › test_rules[differs from REQ-002]"),
    ],
)
def test_strict_still_refuses_the_same_id_outside_test_case_items(tmp_path, capsys, page, line):
    overrides = dict(MULTI_LINE)
    if page == "Home.md":
        overrides[page] = f"# Home\n\n<!-- page-kind: note -->\n{line}\n"
    else:
        overrides[page] = TEST1_ML.replace("- `scripts/tests/test_two.py`\n", f"- `scripts/tests/test_two.py`\n\n{line}\n")
    code, files = run(split_wiki(tmp_path / "wiki", overrides), tmp_path, "--strict")
    assert code == 1
    assert files == {}
    assert f"{page}: unlinked mention of REQ-002" in errors(capsys)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {"tests/TEST-001.md": TEST1_ML.replace(STIMULUS, "Step one.\n\n~~~\nSee [[Missing]] and REQ-002.\n~~~")},
            "tests/TEST-001.md: section 'Stimulus' contains a code fence line on line 3; a table cell cannot hold a fenced block",
        ),
        (
            {"tests/TEST-001.md": TEST1_ML.replace(STIMULUS, "Step one.\n```sh\nrun [[Arch#context-view]]\n```")},
            "tests/TEST-001.md: section 'Stimulus' contains a code fence line on line 2",
        ),
        (
            # Unclosed: on the page it would hide the later Required observation, and its bare ID, from --strict.
            {"tests/TEST-001.md": TEST1_ML.replace(STIMULUS, "Step one.\n```").replace("[[REQ-001]]", "REQ-002")},
            "tests/TEST-001.md: section 'Stimulus' contains a code fence line on line 2",
        ),
        (
            {"tests/TEST-001.md": TEST1_ML.replace(STIMULUS, "Step one.\n   ~~~ indented\nStep two.")},
            "tests/TEST-001.md: section 'Stimulus' contains a code fence line on line 2",
        ),
        (
            {"tests/TEST-001.md": TEST1_ML.replace(STIMULUS, "~~~")},
            "tests/TEST-001.md: section 'Stimulus' contains a code fence line; a table cell cannot hold a fenced block",
        ),
        (
            {"req/REQ-001.md": REQ1.replace("The engine shall do one thing.", "```one thing.```")},
            "req/REQ-001.md: section 'Requirement' contains a code fence line; a table cell",
        ),
        (
            {"req/REQ-001.md": REQ1.replace("# REQ-001: First", "# REQ-001: ~~~First")},
            "req/REQ-001.md: title contains a code fence line; a table cell",
        ),
    ],
)
def test_code_fence_lines_are_refused(tmp_path, capsys, overrides, message):
    code, files = run(split_wiki(tmp_path / "wiki", {**MULTI_LINE, **overrides}), tmp_path, "--strict")
    assert code == 1
    assert files == {}
    assert message in errors(capsys)


def test_triple_backticks_inside_a_line_stay_allowed(tmp_path, capsys):
    page = TEST1_ML.replace(STIMULUS, "Step one.\n\nRun ```a `b` c``` and `d`.")
    code, files = run(split_wiki(tmp_path / "wiki", {**MULTI_LINE, "tests/TEST-001.md": page}), tmp_path, "--strict")
    assert code == 0, errors(capsys)
    assert "| Step one.<br><br>Run ```a `b` c``` and `d`. | " in files["overview.md"]


ITEM_CLAIM = "- `scripts/tests/test_x.py` › {}"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {"req/REQ-001.md": REQ1_ML.replace("## TestID\n\n[[TEST-001]]\n", f"## TestID\n\n[[TEST-001]]\n\n{ITEM_CLAIM.format('TEST-002')}\n")},
            "req/REQ-001.md: unlinked mention of TEST-002",
        ),
        (
            {"req/REQ-001.md": REQ1_ML.replace("in [[Planned-Cases]]", f"in [[Planned-Cases]]\n{ITEM_CLAIM.format('TEST-002')}")},
            "req/REQ-001.md: unlinked mention of TEST-002",
        ),
        (
            {"tests/TEST-001.md": TEST1_ML.replace(STIMULUS, f"{STIMULUS}\n3. `scripts/tests/test_x.py` › REQ-002")},
            "tests/TEST-001.md: unlinked mention of REQ-002",
        ),
    ],
    ids=["req-testid", "req-verification", "test-stimulus"],
)
def test_test_case_item_exemption_is_only_for_required_observation(tmp_path, capsys, overrides, message):
    code, files = run(split_wiki(tmp_path / "wiki", {**MULTI_LINE, **overrides}), tmp_path, "--strict")
    assert code == 1
    assert files == {}
    assert message in errors(capsys)
