#!/usr/bin/env python3
"""Generate and check the test-case items of a wiki's test pages (rsr/ecosystem-wiki-standard@8).

Managed by the repo-standards-review consumer kit (templates/consumer); update it
with `cruft update`. It needs only Python 3.9+ and Git.

Commands:
- `evidence` turns a test runner's report into execution evidence
  (`rsr/test-evidence@1`). Run the tests in a clean checkout of the member
  commit; the evidence records that commit and a platform label. It reads
  Jest-compatible JSON reports, such as `vitest run --reporter=json`, or with
  `--format pytest-junit` pytest's `--junitxml` report, where the node ID
  `file::Class::test[param]` becomes `` `file` › Class › test[param] ``.
- `stimulus` writes a repeatable stimulus for each test page whose stimulus names
  no test file that the evidence ran, from the test files that the evidence ran
  and that the `TestArtifact` sections of the requirements linking the page
  name. Each step is a given command; `{files}` in the last one becomes those
  files. A second run changes nothing.
- `generate` writes the test-case items of each test page whose stimulus names
  a test file that the evidence ran: every test case of those files, in runner
  order, as `` `file` › describe chain › title ``. When that would put the page
  over the 16,384-byte page budget or the 16,384-character registry-text budget,
  it names whole files instead (`` `file` ``, a file item), largest first, until
  the page fits. A page without test-case items gets a new Required observation;
  otherwise only its list of test-case items is replaced. A link from a requirement's
  `TestID` section asserts no coverage: the new observation's lead-in declares the linked
  requirements `not verified here`. Only with `--coverage-map <file.json>`, a person's
  `{"<test page ID>": ["<requirement ID>", ...]}`, does a page say `Together they cover`
  for the mapped requirements; one that the page does not link, a malformed file or an
  unknown page ID is refused with exit 2. The output depends
  only on the pages and the evidence, so a second run changes nothing.
- `check` reads the test pages of one wiki commit and reports each test-case item
  that names no test case the evidence lists for its file
  (`observation-test-missing`), that adds words to one (`observation-not-verbatim`),
  or whose test case, or for a file item any test case of its file, the evidence
  does not show passing (`observation-not-passing`). It also reports a page whose Required
  observation says `Together they cover` for a requirement that no coverage map names
  (`observation-overclaim`): without `--coverage-map <file.json>`, the same file that `generate`
  reads, no page may say it. With `--anchored` (off by default, so a
  wiki is judged against the `@8` anchoring rule only when it opts in) it also reports every
  other claim of a Required observation (`observation-unanchored`): a list item or a paragraph
  that is not a test-case item and does not directly precede a list. Only a quote item is anchored: its whole
  text is one inline code span whose content is a verbatim substring of a tracked file that the
  stimulus names, read at the member commit from `--member-root`, a checkout at the evidence
  commit; `--member-root` needs `--anchored`. Without `--member-root` no quote can be verified. Exit codes and output lines cannot
  be verified until the evidence records step output.

Exit 0 on success, 1 with findings or refused pages, 2 when the command cannot run.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ElementTree
import xml.parsers.expat
from pathlib import Path, PurePosixPath

EVIDENCE_SCHEMA = "rsr/test-evidence@1"
EVIDENCE_KEYS = {"schema", "commit", "platform", "format", "cases"}
PROFILE_FILE = ".wiki-profile.json"
PROFILE_SCHEMAS = ("rsr/wiki-profile@1", "rsr/wiki-profile@2")
SEPARATOR = " \u203a "
LEAD_IN = "Each test case below passes at the member commit."
# A test file that its runner reports but cannot load counts as one failing test case of this name.
LOAD_ERROR = "(load error)"
# The ecosystem wiki standard's page budget (bytes) and registry-text budget (characters).
BUDGET = 16384
# Jest and vitest assertion statuses; anything that did not run counts as skipped.
STATUSES = {"passed": "passed", "failed": "failed", "skipped": "skipped", "pending": "skipped", "todo": "skipped",
            "disabled": "skipped"}
# Combining the cases a runner reports under one name on one platform: a failure outweighs a pass, which outweighs a skip.
COMBINE = {"skipped": 0, "passed": 1, "failed": 2}
REQUIREMENT_SECTIONS = ["Requirement", "Rationale", "Fit Criterion", "Verification", "Source", "Priority", "TestID",
                        "TestArtifact", "CodeRef", "Status"]
DEFAULT_PROFILE = [
    {"scope": "system", "directory": "system-requirements", "prefixes": ["SYS-REQ"], "sections": REQUIREMENT_SECTIONS},
    {"scope": "software", "directory": "software-requirements", "prefixes": ["REQ"], "sections": REQUIREMENT_SECTIONS},
    {"scope": "test", "directory": "tests", "prefixes": ["TEST"], "sections": ["Stimulus", "Required observation"]},
]
ID = re.compile(r"[A-Z][A-Z0-9]*(?:-[A-Z][A-Z0-9]*)*-[0-9]{3,}")
COMMIT = re.compile(r"[0-9a-f]{40}")
SHORT_COMMIT = re.compile(r"[0-9a-f]{7,40}")
PLATFORM = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
HEADING = re.compile(r"\n\n## ([^\n]*)\n\n")
ITEM = re.compile(r"(?:[-*+]|[0-9]{1,9}[.)])[ \t]+(.*)")
ITEM_FILE = re.compile(r"`([^`\n]+)`")
QUOTE = re.compile(r"\s*`([^`\n]+)`\s*")
LINK = re.compile(r"\[\[([^\]|#\n]+)")
TOKEN = re.compile(r"[^\s`'\"()\[\]{},;]+")
CODE = re.compile(r"```.*?```|~~~.*?~~~|`[^`\n]+`", re.S)
MEMBER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
# A stimulus names its files unquoted, so that a later run finds them again.
PLAIN_FILE = re.compile(r"[A-Za-z0-9_./+-]+")
FORMATS = ("jest-json", "pytest-junit")


class Refused(Exception):
    """The command cannot run (exit 2)."""


class Evidence:
    """Test cases from one or more evidence files of the same commit, one file per platform."""

    def __init__(self, commit: str, platforms: list[str], order: dict, statuses: dict) -> None:
        self.commit = commit
        self.platforms = platforms
        self.order = order  # file -> test-case names in runner order
        self.statuses = statuses  # (file, name) -> {platform: status}


def git(directory: Path, arguments: list[str], stdin: bytes | None = None) -> bytes:
    completed = subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", str(directory), *arguments],
                               input=stdin, capture_output=True, check=False)
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", "replace").strip().splitlines()
        raise Refused(f"git {arguments[0]} failed: {message[-1] if message else completed.returncode}")
    return completed.stdout


def safe_relative(path: object) -> bool:
    if not isinstance(path, str) or not path or "\\" in path or "`" in path or path.startswith("/"):
        return False
    if any(character in path for character in "\n\r\t\0"):
        return False
    return all(segment not in ("", ".", "..") for segment in path.split("/"))


def evidence_problem(document: object) -> str | None:
    if not isinstance(document, dict) or set(document) != EVIDENCE_KEYS:
        return f"not {EVIDENCE_SCHEMA}: the keys must be {', '.join(sorted(EVIDENCE_KEYS))}"
    if document["schema"] != EVIDENCE_SCHEMA:
        return f"schema is {document['schema']!r}, not {EVIDENCE_SCHEMA}"
    if not isinstance(document["commit"], str) or not COMMIT.fullmatch(document["commit"]):
        return "commit is not a full lower-case commit ID"
    if not isinstance(document["platform"], str) or not PLATFORM.fullmatch(document["platform"]):
        return f"platform must match {PLATFORM.pattern}"
    if not isinstance(document["format"], str) or not isinstance(document["cases"], list):
        return "format must be a string and cases a list"
    for case in document["cases"]:
        if not isinstance(case, dict) or set(case) != {"file", "name", "status"}:
            return "each case has exactly the keys file, name and status"
        if not safe_relative(case["file"]):
            return f"case file {case['file']!r} is not a relative /-separated path"
        if not isinstance(case["name"], str) or not case["name"].strip() or "\n" in case["name"] or "\r" in case["name"]:
            return f"case name {case['name']!r} in {case['file']} is empty or spans lines"
        if not isinstance(case["status"], str) or case["status"] not in COMBINE:
            return f"case status {case['status']!r} is not passed, failed or skipped"
    return None


def load_evidence(paths: list[str]) -> Evidence:
    documents = []
    for path in paths:
        try:
            document = json.loads(Path(path).read_text(encoding="utf-8"))
        except ValueError as error:
            raise Refused(f"{path}: not JSON: {error}") from None
        problem = evidence_problem(document)
        if problem:
            raise Refused(f"{path}: {problem}")
        documents.append(document)
    commits = sorted({document["commit"] for document in documents})
    if len(commits) != 1:
        raise Refused(f"the evidence files record different commits: {', '.join(commits)}")
    platforms = [document["platform"] for document in documents]
    if len(set(platforms)) != len(platforms):
        raise Refused("give one evidence file per platform")
    order: dict[str, list[str]] = {}
    statuses: dict[tuple[str, str], dict[str, str]] = {}
    for document in sorted(documents, key=lambda item: item["platform"]):
        platform = document["platform"]
        for case in document["cases"]:
            key = (case["file"], case["name"])
            if key not in statuses:
                order.setdefault(case["file"], []).append(case["name"])
                statuses[key] = {}
            # Several cases under one name on one platform: any failure shows it failing, else any pass shows it passing.
            previous = statuses[key].get(platform, "skipped")
            statuses[key][platform] = max(previous, case["status"], key=COMBINE.__getitem__)
    return Evidence(commits[0], sorted(platforms), order, statuses)


def load_profile(raw: bytes | None) -> list[dict]:
    if raw is None:
        return DEFAULT_PROFILE
    try:
        profile = json.loads(raw.decode("utf-8"))
    except ValueError:
        profile = None
    if not isinstance(profile, dict) or profile.get("schema") not in PROFILE_SCHEMAS \
            or not isinstance(profile.get("scopes"), list):
        raise Refused(f"{PROFILE_FILE} is not a valid wiki profile; the conformance checker reports why")
    scopes = []
    for scope in profile["scopes"]:
        if not isinstance(scope, dict):
            raise Refused(f"{PROFILE_FILE} is not a valid wiki profile; the conformance checker reports why")
        prefixes = scope.get("idPrefixes", [scope.get("idPrefix")])
        sections = scope.get("sections")
        if not isinstance(scope.get("scope"), str) or not isinstance(scope.get("directory"), str) \
                or not isinstance(prefixes, list) or not prefixes or not all(isinstance(p, str) for p in prefixes) \
                or not isinstance(sections, list) or not all(isinstance(s, str) for s in sections):
            raise Refused(f"{PROFILE_FILE} is not a valid wiki profile; the conformance checker reports why")
        scopes.append({"scope": scope["scope"], "directory": scope["directory"], "prefixes": prefixes,
                       "sections": sections})
    return scopes


def test_scope(scopes: list[dict]) -> dict:
    for scope in scopes:
        if scope["scope"] == "test" and len(scope["sections"]) >= 2:
            return scope
    raise Refused("the wiki profile has no test scope with a stimulus and a required observation")


def has_prefix(identifier: str, prefixes: list[str]) -> bool:
    return ID.fullmatch(identifier) is not None and identifier.rsplit("-", 1)[0] in prefixes


def parse_page(text: str, identifier: str, sections: list[str]) -> list[tuple[int, int]] | None:
    """Each section body's offsets in a canonical section page, or None for any other page."""
    title = text.split("\n", 1)[0]
    if not title.startswith("# ") or title[2:].split(":", 1)[0] != identifier or not text.endswith("\n"):
        return None
    headings = list(HEADING.finditer(text, len(title)))
    if not headings or headings[0].start() != len(title) or [h.group(1) for h in headings] != sections:
        return None
    ends = [heading.start() for heading in headings[1:]] + [len(text) - 1]
    return [(heading.end(), end) for heading, end in zip(headings, ends)]


def list_items(body: str) -> list[list]:
    """Each list item, nested or not: [first line, last line, text with its continuation lines, indentation]."""
    items: list[list] = []
    current = None
    blank = False
    fence = None
    for index, line in enumerate(body.split("\n")):
        stripped = line.lstrip(" \t")
        indent = len(line) - len(stripped)
        code = fence is not None
        if code and stripped.startswith(fence):
            fence = None
        elif not code and stripped.startswith(("```", "~~~")):
            fence, code = stripped[:3], True
        match = None if code else ITEM.fullmatch(stripped)
        if match:
            current = [index, index, match.group(1), indent]
            items.append(current)
        elif not stripped:
            blank = True
            continue
        elif current is not None and (not blank or indent > current[3]):
            current[1] = index
            current[2] += "\n" + line
        else:
            current = None
        blank = False
    return items


def test_case(text: str, files) -> tuple[str, str] | None:
    """The file and the rest of a test-case item, or None for any other item."""
    match = ITEM_FILE.match(text)
    if match is None:
        return None
    file, rest = match.group(1), text[match.end():]
    return (file, rest) if "\u203a" in rest or file in files else None


def test_case_items(body: str, files, named: tuple = ()) -> list[tuple[int, int, int, str, str]]:
    """Each test-case item of a body as (first line, last line, indent, file, rest).

    A file item for a path that the stimulus names but no evidence ran also counts when it is in the same list as
    another test-case item, or in a list that directly follows a paragraph beginning with the lead-in sentence, so
    that a file missing from the evidence is still judged.
    """
    lines = body.split("\n")
    # Group items by list: a deeper item opens a nested list, and a paragraph at or above a level ends its list.
    groups: list[list] = []
    open_lists: list[tuple[int, int]] = []
    previous = -1
    for first, last, text, indent in list_items(body):
        gap = [line for line in lines[previous + 1:first] if line.strip()]
        if gap:
            outdent = min(len(line) - len(line.lstrip(" \t")) for line in gap)
            while open_lists and open_lists[-1][0] >= outdent:
                open_lists.pop()
        while open_lists and open_lists[-1][0] > indent:
            open_lists.pop()
        strict = test_case(text, files)
        match = ITEM_FILE.match(text)
        loose = (match.group(1), "") if strict is None and match and text[match.end():] == "" \
            and match.group(1) in named else None
        entry = (first, last, indent, strict, loose)
        if open_lists and open_lists[-1][0] == indent:
            groups[open_lists[-1][1]].append(entry)
        else:
            groups.append([entry])
            open_lists.append((indent, len(groups) - 1))
        previous = last
    found = []
    for group in groups:
        before = group[0][0] - 1
        while before >= 0 and not lines[before].strip():
            before -= 1
        start = before
        while start > 0 and lines[start - 1].strip():
            start -= 1
        led = before >= 0 and lines[start].lstrip().startswith(LEAD_IN)
        listed = led or any(entry[3] for entry in group)
        for first, last, indent, strict, loose in group:
            part = strict or (loose if listed else None)
            if part:
                found.append((first, last, indent, *part))
    return found


def stimulus_tokens(stimulus: str) -> list[str]:
    tokens: list[str] = []
    for token in TOKEN.findall(stimulus):
        candidate = (token[2:] if token.startswith("./") else token).rstrip(".:")
        if candidate and candidate not in tokens:
            tokens.append(candidate)
    return tokens


def stimulus_files(stimulus: str, files: dict) -> list[str]:
    """The evidence's test files that the stimulus names, in the order it first names them."""
    return [token for token in stimulus_tokens(stimulus) if token in files]


def stimulus_runs(stimulus: str, file: str, files: dict) -> bool | None:
    """Whether the stimulus names the file or a directory that holds it; None when it names no evidence file or directory.

    A directory counts only inside code, where a step names a command's arguments, so prose such as "Run the tests"
    names no directory.
    """
    tokens = stimulus_tokens(stimulus)
    directories = [token.rstrip("/") + "/" for token in stimulus_tokens(" ".join(CODE.findall(stimulus)))]
    if not any(token in files for token in tokens) \
            and not any(name.startswith(directory) for directory in directories for name in files):
        return None
    return file in tokens or any(file.startswith(directory) for directory in directories)


def join_ids(identifiers: list[str]) -> str:
    return identifiers[0] if len(identifiers) == 1 else f"{', '.join(identifiers[:-1])} and {identifiers[-1]}"


def read_text(path: Path) -> tuple[str, bool] | None:
    """A working-tree page with LF line endings, and whether it was checked out with CRLF."""
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text.replace("\r\n", "\n"), "\r\n" in text


def coverage(wiki: Path, scopes: list[dict], test: dict) -> dict[str, list[str]]:
    """Test ID -> the requirement IDs whose TestID section links it."""
    covered: dict[str, set] = {}
    for scope in scopes:
        if scope["scope"] not in ("system", "software") or "TestID" not in scope["sections"]:
            continue
        for path in sorted((wiki / scope["directory"]).glob("*.md")):
            read = read_text(path)
            spans = None if read is None else parse_page(read[0], path.stem, scope["sections"])
            if spans is None:
                if read is None or not read[0].split("\n", 2)[-1].startswith("<!-- page-kind:"):
                    print(f"NOTE: {path}: not a canonical section page, so its TestID links are not read")
                continue
            start, end = spans[scope["sections"].index("TestID")]
            for link in LINK.findall(read[0][start:end]):
                if has_prefix(link.strip(), test["prefixes"]):
                    covered.setdefault(link.strip(), set()).add(path.stem)
    return {identifier: sorted(requirements) for identifier, requirements in covered.items()}


def over_budget(text: str, identifier: str, sections: list[str]) -> bool:
    """Whether a canonical page exceeds the page budget or its registry text the registry-text budget."""
    spans = parse_page(text, identifier, sections)
    if spans is None:
        return True
    fields = {"ID": identifier}
    after = text.split("\n", 1)[0][2 + len(identifier):]
    if after.startswith(": "):
        fields["Title"] = after[2:]
    for heading, (start, end) in zip(sections, spans):
        fields[heading] = text[start:end]
    # The registry text is the page's fields as compact JSON with keys in code-point order.
    registry = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return len(text.encode("utf-8")) > BUDGET or len(registry) > BUDGET


def item_lines(files: list[str], evidence: Evidence, whole: set) -> list[str]:
    return [line for file in files for line in ([f"- `{file}`"] if file in whole else
                                                [f"- `{file}`{SEPARATOR}{name}" for name in evidence.order[file]])]


def lead_in_for(linked: list[str], verified: list[str]) -> str:
    """The lead-in: coverage only for requirements that a person mapped; every other linked one is declared unverified."""
    sentences = [LEAD_IN]
    verified = [identifier for identifier in linked if identifier in verified]
    if verified:
        sentences.append(f"Together they cover {join_ids(verified)}.")
    rest = [identifier for identifier in linked if identifier not in verified]
    if rest:
        sentences.append(f"Linked requirements, not verified here: {join_ids(rest)}.")
    return " ".join(sentences)


COVER_SENTENCE = re.compile(r"Together\s+they\s+cover\s+([^.]*)\.")


def overclaims(observation: str, mapped: list[str]) -> list[tuple[int, str]]:
    """Each `Together they cover` sentence of an observation, with the IDs it claims that no map names."""
    found = []
    for match in COVER_SENTENCE.finditer(observation):
        claimed = [word for word in re.split(r",?\s+and\s+|,\s*", match.group(1).strip()) if word]
        unmapped = [identifier for identifier in claimed if identifier not in mapped]
        if unmapped:
            found.append((observation.count("\n", 0, match.start()), ", ".join(unmapped)))
    return found


def load_coverage_map(path: str, pages: list[Path], covered: dict[str, list[str]]) -> dict[str, list[str]]:
    """Test page ID -> the linked requirement IDs that a person says its items verify."""
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not all(
            isinstance(key, str) and isinstance(value, list) and value
            and all(isinstance(item, str) for item in value) and len(set(value)) == len(value)
            for key, value in document.items()):
        raise Refused(f"{path}: not a JSON object of test page ID -> non-empty list of distinct requirement IDs")
    known = {page.stem for page in pages}
    unknown = sorted(set(document) - known)
    if unknown:
        raise Refused(f"{path}: no test page {', '.join(unknown)}")
    for identifier, requirements in sorted(document.items()):
        unlinked = [item for item in requirements if item not in covered.get(identifier, [])]
        if unlinked:
            raise Refused(f"{path}: {identifier} does not link {', '.join(unlinked)} through a TestID section")
    return document


def generated_body(body: str, items: list[str], requirements: list[str], evidence: Evidence,
                   named: tuple = (), verified: list[str] = ()) -> str | None:
    """The new required observation, or None when the body holds more than one list of test-case items."""
    lines = body.split("\n")
    blocks: list[list[int]] = []
    for first, last, indent, _, _ in test_case_items(body, evidence.order, named):
        # Test-case items separated only by blank lines are one list.
        if blocks and not any(lines[number].strip() for number in range(blocks[-1][1] + 1, first)):
            blocks[-1][1] = last
        else:
            blocks.append([first, last, indent])
    if len(blocks) > 1:
        return None
    if blocks:
        first, last, indent = blocks[0]
        prefix = lines[first][:indent]
        return "\n".join(lines[:first] + [prefix + item for item in items] + lines[last + 1:])
    lead_in = lead_in_for(requirements, list(verified))
    return f"{lead_in}\n\n" + "\n".join(items)


def page_ids(paths: list, ids: list[str], stem) -> list:
    if not ids:
        return paths
    known = {stem(path): path for path in paths}
    missing = [identifier for identifier in ids if identifier not in known]
    if missing:
        raise Refused(f"no test page {', '.join(missing)}")
    return [known[identifier] for identifier in sorted(set(ids))]


def jest_cases(report_path: Path, top: Path, tracked: set, commit: str) -> list[dict]:
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except ValueError as error:
        raise Refused(f"{report_path}: not JSON: {error}") from None
    results = report.get("testResults") if isinstance(report, dict) else None
    if not isinstance(results, list):
        raise Refused(f"{report_path} has no testResults list; give a Jest-compatible JSON report")
    cases = []
    for result in results:
        name = result.get("name") if isinstance(result, dict) else None
        assertions = result.get("assertionResults") if isinstance(result, dict) else None
        if not isinstance(name, str) or not isinstance(assertions, list):
            raise Refused(f"{report_path}: each test result needs a name and assertionResults")
        path = Path(name) if Path(name).is_absolute() else top / name
        try:
            relative = Path(os.path.realpath(path)).relative_to(top).as_posix()
        except ValueError:
            raise Refused(f"{name} is outside the checkout {top}") from None
        if relative not in tracked:
            raise Refused(f"{relative} is not in the member commit {commit[:12]}; commit it or remove it")
        if not assertions:
            if result.get("status") == "failed":
                # The runner could not load the file, so none of its test cases ran.
                cases.append({"file": relative, "name": LOAD_ERROR, "status": "failed"})
                sys.stderr.write(f"NOTE: {relative} failed to load; it counts as one failing test case\n")
            else:
                sys.stderr.write(f"NOTE: {relative} reported no test cases\n")
        for assertion in assertions:
            titles = assertion.get("ancestorTitles") if isinstance(assertion, dict) else None
            title = assertion.get("title") if isinstance(assertion, dict) else None
            status = assertion.get("status") if isinstance(assertion, dict) else None
            if not isinstance(titles, list) or not all(isinstance(t, str) for t in titles) or not isinstance(title, str):
                raise Refused(f"{relative}: each assertion result needs ancestorTitles and a title")
            if not isinstance(status, str) or status not in STATUSES:
                raise Refused(f"{relative}: unknown test status {status!r}")
            cases.append({"file": relative, "name": SEPARATOR.join([*titles, title]), "status": STATUSES[status]})
    return cases


def refuse_document_type(*_: object) -> None:
    raise Refused("a report with a document type or entity declarations is refused")


def module_file(dotted: str, tracked: set, declared: str | None, commit: str) -> tuple[str, int]:
    """The tracked .py file a dotted pytest path names, and how many of its parts the file takes."""
    parts = dotted.split(".")
    candidates = [("/".join(parts[:size]) + ".py", size) for size in range(1, len(parts) + 1)]
    matches = [candidate for candidate in candidates if candidate[0] in tracked]
    if len(matches) > 1 and declared is not None:
        # The file attribute only chooses between candidates: xunit1 reports an inherited test's defining module.
        matches = [candidate for candidate in matches if candidate[0] == declared.replace("\\", "/")] or matches
    if len(matches) != 1:
        reason = "no file" if not matches else f"{len(matches)} files"
        raise Refused(f"{dotted} names {reason} of the member commit {commit[:12]}; run pytest from the checkout's root")
    return matches[0]


def pytest_cases(report_path: Path, top: Path, tracked: set, commit: str) -> list[dict]:
    """Test cases from pytest's JUnit XML report, made from the checkout's root."""
    raw = report_path.read_bytes()
    # A runner's report never needs a document type. Refusing one at the parser, in any encoding, rules out entity
    # expansion before ElementTree reads the report.
    guard = xml.parsers.expat.ParserCreate()
    guard.StartDoctypeDeclHandler = refuse_document_type
    guard.EntityDeclHandler = refuse_document_type
    try:
        guard.Parse(raw, True)
        root = ElementTree.fromstring(raw)
    except Refused as error:
        raise Refused(f"{report_path}: {error}") from None
    except (xml.parsers.expat.ExpatError, ElementTree.ParseError, LookupError, ValueError) as error:
        raise Refused(f"{report_path}: not readable XML: {error}") from None
    if root.tag not in ("testsuites", "testsuite"):
        raise Refused(f"{report_path}: not a JUnit report; give pytest --junitxml output")
    cases = []
    for case in root.iter("testcase"):
        classname, name = case.get("classname"), case.get("name")
        if classname is None or not name:
            raise Refused(f"{report_path}: each testcase needs a classname and a name")
        tags = {child.tag for child in case}
        status = "failed" if tags & {"failure", "error"} else "skipped" if "skipped" in tags else "passed"
        if not classname:
            # pytest reports a module it could not collect as a test case named after the module.
            file, _ = module_file(name, tracked, None, commit)
            cases.append({"file": file, "name": LOAD_ERROR, "status": "failed"})
            sys.stderr.write(f"NOTE: pytest could not collect {file}; it counts as one failing test case\n")
            continue
        file, size = module_file(classname, tracked, case.get("file"), commit)
        cases.append({"file": file, "name": SEPARATOR.join([*classname.split(".")[size:], name]), "status": status})
    return cases


def command_evidence(options: argparse.Namespace) -> int:
    if not PLATFORM.fullmatch(options.platform):
        raise Refused(f"--platform must match {PLATFORM.pattern}, for example linux-x64")
    top = Path(os.path.realpath(git(Path(options.root), ["rev-parse", "--show-toplevel"]).decode().strip()))
    commit = git(top, ["rev-parse", "HEAD"]).decode().strip()
    if git(top, ["status", "--porcelain", "--untracked-files=no"]).strip():
        raise Refused("tracked files differ from HEAD; run the tests in a clean checkout of the member commit")
    tracked = set(git(top, ["ls-tree", "-r", "-z", "--name-only", "HEAD"]).decode("utf-8", "surrogateescape").split("\0"))
    read = pytest_cases if options.format == "pytest-junit" else jest_cases
    cases = read(Path(options.report), top, tracked, commit)
    document = {"schema": EVIDENCE_SCHEMA, "commit": commit, "platform": options.platform, "format": options.format,
                "cases": cases}
    problem = evidence_problem(document)
    if problem:
        raise Refused(problem)
    Path(options.output).write_bytes((json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    files = len({case["file"] for case in cases})
    passed = sum(case["status"] == "passed" for case in cases)
    print(f"PASS: wrote {len(cases)} test cases ({passed} passed) from {files} files at {commit[:12]} "
          f"on {options.platform} to {options.output}")
    return 0


def command_generate(options: argparse.Namespace) -> int:
    wiki = Path(options.wiki)
    evidence = load_evidence(options.evidence)
    profile = wiki / PROFILE_FILE
    scopes = load_profile(profile.read_bytes() if profile.is_file() else None)
    test = test_scope(scopes)
    covered = coverage(wiki, scopes, test)
    every = sorted((wiki / test["directory"]).glob("*.md"))
    mapped = load_coverage_map(options.coverage_map, every, covered) if options.coverage_map else {}
    pages = page_ids(every, options.ids, lambda path: path.stem)
    updates, refused, plain = {}, [], 0
    for path in pages:
        read = read_text(path)
        text, crlf = read if read is not None else ("", False)
        spans = None if read is None else parse_page(text, path.stem, test["sections"])
        if spans is None:
            if options.ids:
                refused.append(f"{path}: not a canonical section page")
            elif read is None or not text.split("\n", 2)[-1].startswith("<!-- page-kind:"):
                print(f"NOTE: {path}: not a canonical section page; the conformance checker reports it")
            continue
        (stimulus_start, stimulus_end), (start, end) = spans[0], spans[1]
        files = stimulus_files(text[stimulus_start:stimulus_end], evidence.order)
        if not files:
            plain += 1
            if path.stem in mapped:
                print(f"NOTE: {path}: mapped in --coverage-map, but its stimulus names no test file that the evidence ran, so nothing was written")
            continue
        # Largest files first, then stimulus order, so the choice depends only on the page and the evidence.
        largest = sorted(files, key=lambda file: (-len(evidence.order[file]), files.index(file)))
        whole: set = set()
        while True:
            body = generated_body(text[start:end], item_lines(files, evidence, whole), covered.get(path.stem, []),
                                  evidence, tuple(stimulus_tokens(text[stimulus_start:stimulus_end])),
                                  mapped.get(path.stem, []))
            if body is None:
                break
            updated = text[:start] + body + text[end:]
            if not over_budget(updated, path.stem, test["sections"]) or len(whole) == len(files):
                break
            whole.add(largest[len(whole)])
        if path.stem in mapped and test_case_items(text[start:end], evidence.order,
                                                    tuple(stimulus_tokens(text[stimulus_start:stimulus_end]))):
            print(f"NOTE: {path}: mapped in --coverage-map, but it already lists test cases, so its lead-in is kept")
        if body is None:
            refused.append(f"{path}: the required observation holds more than one list of test-case items")
            continue
        if over_budget(updated, path.stem, test["sections"]):
            refused.append(f"{path}: the page exceeds the page or registry-text budget even with file items")
            continue
        if updated != text:
            updates[path] = updated.replace("\n", "\r\n") if crlf else updated
    for message in refused:
        print(f"REFUSED: {message}")
    if refused:
        print("FAIL: no page was written")
        return 1
    for path in sorted(updates):
        print(f"{'WOULD UPDATE' if options.dry_run else 'UPDATED'}: {path}")
        if not options.dry_run:
            path.write_bytes(updates[path].encode("utf-8"))
    print(f"{'FAIL' if options.dry_run and updates else 'PASS'}: {len(updates)} pages "
          f"{'to update' if options.dry_run else 'updated'}, {plain} pages name no test file that the evidence ran")
    return 1 if options.dry_run and updates else 0


def linked_sections(wiki: Path, scopes: list[dict], test: dict, section: str) -> dict[str, list[tuple[str, str]]]:
    """Test ID -> (requirement ID, the text of its section) for each requirement whose TestID section links it."""
    linked: dict[str, list[tuple[str, str]]] = {}
    for scope in scopes:
        if scope["scope"] not in ("system", "software") or "TestID" not in scope["sections"] \
                or section not in scope["sections"]:
            continue
        for path in sorted((wiki / scope["directory"]).glob("*.md")):
            read = read_text(path)
            spans = None if read is None else parse_page(read[0], path.stem, scope["sections"])
            if spans is None:
                continue
            start, end = spans[scope["sections"].index("TestID")]
            body = read[0][slice(*spans[scope["sections"].index(section)])]
            for link in dict.fromkeys(link.strip() for link in LINK.findall(read[0][start:end])):
                if has_prefix(link, test["prefixes"]):
                    linked.setdefault(link, []).append((path.stem, body))
    return linked


def join_words(words: list[str]) -> str:
    return words[0] if len(words) == 1 else f"{', '.join(words[:-1])} and {words[-1]}"


def stimulus_body(evidence: Evidence, member: str, steps: list[str], files: list[str]) -> str:
    platforms = sorted({platform for file in files for name in evidence.order[file]
                        for platform in evidence.statuses[(file, name)]})
    owner = f"{member} " if member else ""
    lines = [f"Member commit: {owner}`{evidence.commit}`. Run these steps in a clean checkout of it, "
             f"on {join_words(platforms)}:", ""]
    arguments = " ".join(files)
    for number, step in enumerate(steps, 1):
        lines.append(f"{number}. `{step.replace('{files}', arguments)}`")
    return "\n".join(lines)


def command_stimulus(options: argparse.Namespace) -> int:
    steps = [*options.step, options.last_step]
    if any(not step.strip() or "\n" in step or "\r" in step or "`" in step for step in steps):
        raise Refused("each step is one line of shell without backticks")
    if options.last_step.count("{files}") != 1 or any("{files}" in step for step in options.step):
        raise Refused("--command must hold {files} exactly once, and no --step may hold it")
    if options.member and not MEMBER.fullmatch(options.member):
        raise Refused(f"--member must match {MEMBER.pattern}")
    wiki = Path(options.wiki)
    evidence = load_evidence(options.evidence)
    profile = wiki / PROFILE_FILE
    scopes = load_profile(profile.read_bytes() if profile.is_file() else None)
    test = test_scope(scopes)
    linked = linked_sections(wiki, scopes, test, options.artifact_section)
    pages = page_ids(sorted((wiki / test["directory"]).glob("*.md")), options.ids, lambda path: path.stem)
    updates, refused, runnable, unmatched = {}, [], 0, []
    for path in pages:
        read = read_text(path)
        text, crlf = read if read is not None else ("", False)
        spans = None if read is None else parse_page(text, path.stem, test["sections"])
        if spans is None:
            if options.ids:
                refused.append(f"{path}: not a canonical section page")
            continue
        start, end = spans[0]
        if stimulus_files(text[start:end], evidence.order):
            runnable += 1
            continue
        files: list[str] = []
        for _, artifact in linked.get(path.stem, []):
            files += [file for file in stimulus_files(artifact, evidence.order) if file not in files]
        if not files:
            unmatched.append(path.stem)
            continue
        unsafe = [file for file in files if not PLAIN_FILE.fullmatch(file)]
        if unsafe:
            refused.append(f"{path}: a step cannot name {', '.join(unsafe)} unquoted")
            continue
        body = stimulus_body(evidence, options.member, steps, files)
        if stimulus_files(body, evidence.order) != files:
            refused.append(f"{path}: the written stimulus would not name its test files again; "
                           "put {files} on its own between spaces")
            continue
        updated = text[:start] + body + text[end:]
        if updated != text:
            updates[path] = updated.replace("\n", "\r\n") if crlf else updated
    for message in refused:
        print(f"REFUSED: {message}")
    if refused:
        print("FAIL: no page was written")
        return 1
    for path in sorted(updates):
        print(f"{'WOULD UPDATE' if options.dry_run else 'UPDATED'}: {path}")
        if not options.dry_run:
            path.write_bytes(updates[path].encode("utf-8"))
    if unmatched:
        print(f"NOTE: {len(unmatched)} pages have no {options.artifact_section} test file that the evidence ran: "
              f"{', '.join(unmatched)}")
    print(f"{'FAIL' if options.dry_run and updates else 'PASS'}: {len(updates)} pages "
          f"{'to update' if options.dry_run else 'updated'}, {runnable} already name a test file that the evidence ran")
    return 1 if options.dry_run and updates else 0


def committed_files(wiki: Path, commit: str) -> dict[str, str]:
    files = {}
    for record in git(wiki, ["ls-tree", "-r", "-z", "--full-tree", commit]).split(b"\0"):
        if record:
            meta, _, path = record.partition(b"\t")
            _, kind, oid = meta.decode("ascii").split()
            if kind == "blob":
                files[path.decode("utf-8", "surrogateescape")] = oid
    return files


def read_blobs(wiki: Path, oids: list[str]) -> list[bytes]:
    if not oids:
        return []
    output = git(wiki, ["cat-file", "--batch"], stdin="".join(f"{oid}\n" for oid in oids).encode("ascii"))
    blobs, position = [], 0
    for oid in oids:
        newline = output.index(b"\n", position)
        header = output[position:newline].split()
        if len(header) != 3 or header[1] != b"blob":
            raise Refused(f"cannot read blob {oid}")
        start = newline + 1
        size = int(header[2])
        blobs.append(output[start:start + size])
        position = start + size + 1
    return blobs


def judge(evidence: Evidence, file: str, rest: str, runs: bool | None) -> tuple[str, str] | None:
    if runs is False:
        return "observation-test-missing", f"the stimulus does not run `{file}`"
    names = evidence.order.get(file)
    if names is None:
        return "observation-not-passing", f"no evidence ran `{file}`"
    if rest == "":
        # A file item: every test case of the file passes.
        failing = [name for name in names if "failed" in evidence.statuses[(file, name)].values()
                   or "passed" not in evidence.statuses[(file, name)].values()]
        if failing:
            return "observation-not-passing", (f"`{file}`: {len(failing)} of {len(names)} test cases do not pass, "
                                               f"first {failing[0]!r}")
        return None
    name = rest[len(SEPARATOR):] if rest.startswith(SEPARATOR) else None
    if name is None or (file, name) not in evidence.statuses:
        contained = [candidate for candidate in names if candidate in rest]
        if not contained:
            shown = rest.strip().replace("\n", " ")
            return "observation-test-missing", f"`{file}` has no test case {shown[:160]!r}"
        return "observation-not-verbatim", f"the item adds words to `{file}`{SEPARATOR}{max(contained, key=len)}"
    statuses = evidence.statuses[(file, name)]
    if "failed" in statuses.values() or "passed" not in statuses.values():
        shown = ", ".join(f"{platform} {status}" for platform, status in sorted(statuses.items()))
        return "observation-not-passing", f"`{file}`{SEPARATOR}{name}: {shown}"
    return None


def claims(body: str, cases: set[int]) -> list[tuple[int, str]]:
    """Each claim of a required observation as (first line, text): a list item that is not a test-case item, and a
    paragraph outside lists and fenced code that does not directly precede a list (a lead-in naming criteria)."""
    lines = body.split("\n")
    items = list_items(body)
    found = [(first, text.strip()) for first, _, text, _ in items if first not in cases]
    taken = {number for first, last, _, _ in items for number in range(first, last + 1)}
    starts = {first for first, _, _, _ in items}
    fence = None
    block: list[int] = []
    blocks: list[list[int]] = []
    for index, line in enumerate(lines):
        stripped = line.lstrip(" \t")
        fenced = fence is not None
        if fenced and stripped.startswith(fence):
            fence = None
        elif not fenced and stripped.startswith(("```", "~~~")):
            fence, fenced = stripped[:3], True
        if fenced or index in taken or not stripped:
            if block:
                blocks.append(block)
            block = []
            if fenced and index not in taken:
                continue
        else:
            block.append(index)
    if block:
        blocks.append(block)
    for block in blocks:
        after = block[-1] + 1
        while after < len(lines) and not lines[after].strip():
            after += 1
        if after not in starts:
            found.append((block[0], " ".join(lines[index].strip() for index in block)))
    return sorted(found)


def quoted_span(claim: str) -> str | None:
    """The content of a quote item: a claim that is exactly one inline code span, a final period allowed."""
    match = QUOTE.fullmatch(claim.rstrip("."))
    return match.group(1) if match else None


def named_contents(member: Path, commit: str, tracked: set, stimulus: str, cache: dict) -> list[str]:
    """The contents, at the member commit, of the tracked files that the stimulus names."""
    contents = []
    for token in stimulus_tokens(stimulus):
        if token in tracked:
            if token not in cache:
                cache[token] = git(member, ["show", f"{commit}:{token}"]).decode("utf-8", "replace")
            contents.append(cache[token])
    return contents


def unanchored(claim: str, contents: list[str] | None) -> str | None:
    """Why a claim is not anchored, or None for a quote that a named file's content contains."""
    shown = " ".join(claim.split())[:80]
    span = quoted_span(claim)
    if span is None:
        return (f"not a test-case item or a single verbatim quote; exit codes and output lines cannot be verified "
                f"until the evidence records step output: {shown!r}")
    if contents is None:
        return f"no --member-root, so the quote cannot be verified: {shown!r}"
    if not any(span in content for content in contents):
        return f"the quote is in no file that the stimulus names at the member commit: {shown!r}"
    return None


def command_check(options: argparse.Namespace) -> int:
    wiki = Path(options.wiki)
    evidence = load_evidence(options.evidence)
    member = options.member_commit.lower()
    if not SHORT_COMMIT.fullmatch(member) or not evidence.commit.startswith(member):
        raise Refused(f"the evidence is from {evidence.commit}, not the member commit {options.member_commit}")
    if options.commit.startswith("-"):
        raise Refused(f"--commit {options.commit!r} is not a commit")
    commit = git(wiki, ["rev-parse", "--verify", f"{options.commit}^{{commit}}"]).decode().strip()
    tree = committed_files(wiki, commit)
    profile = read_blobs(wiki, [tree[PROFILE_FILE]])[0] if PROFILE_FILE in tree else None
    test = test_scope(load_profile(profile))
    paths = sorted(path for path in tree
                   if path.endswith(".md") and PurePosixPath(path).parent.as_posix() == test["directory"])
    paths = page_ids(paths, options.ids, lambda path: PurePosixPath(path).stem)
    if options.member_root is not None and not options.anchored:
        raise Refused("--member-root is only meaningful with --anchored")
    member_root, tracked, cache = None, set(), {}
    if options.member_root is not None:
        member_root = Path(options.member_root)
        head = git(member_root, ["rev-parse", "--verify", "HEAD^{commit}"]).decode().strip()
        if head != evidence.commit:
            raise Refused(f"--member-root is at {head}, not the evidence commit {evidence.commit}")
        tracked = set(committed_files(member_root, head))
    coverage_map = {}
    if options.coverage_map is not None:
        document = json.loads(Path(options.coverage_map).read_text(encoding="utf-8"))
        if not isinstance(document, dict) or not all(
                isinstance(key, str) and isinstance(value, list) and all(isinstance(item, str) for item in value)
                for key, value in document.items()):
            raise Refused(f"{options.coverage_map}: not a JSON object of test page ID -> list of requirement IDs")
        coverage_map = document
    findings, notes, items, pages = [], [], 0, 0
    for path, raw in zip(paths, read_blobs(wiki, [tree[path] for path in paths])):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            notes.append(f"{path}: not UTF-8; the conformance checker reports it")
            continue
        spans = parse_page(text, PurePosixPath(path).stem, test["sections"])
        if spans is None:
            if not text.split("\n", 2)[-1].startswith("<!-- page-kind:"):
                notes.append(f"{path}: not a canonical section page; the conformance checker reports it")
            continue
        (stimulus_start, stimulus_end), (start, end) = spans[0], spans[1]
        first_line = text.count("\n", 0, start) + 1
        named = tuple(stimulus_tokens(text[stimulus_start:stimulus_end]))
        cases = test_case_items(text[start:end], evidence.order, named)
        page_items = [(first_line + first, file, rest) for first, _, _, file, rest in cases]
        stimulus = text[stimulus_start:stimulus_end]
        page_claims = claims(text[start:end], {case[0] for case in cases}) if options.anchored else []
        if not page_items and stimulus_files(stimulus, evidence.order):
            notes.append(f"{path}: its stimulus names a test file that the evidence ran, "
                         "and its required observation lists no test case")
        page_overclaims = overclaims(text[start:end], coverage_map.get(PurePosixPath(path).stem, []))
        if not page_items and not page_claims and not page_overclaims:
            continue
        pages += 1
        page_findings = []
        for line, file, rest in page_items:
            items += 1
            finding = judge(evidence, file, rest, stimulus_runs(stimulus, file, evidence.order))
            if finding is not None:
                page_findings.append((line, f"{path}:{line}: {finding[0]}: {finding[1]}"))
        contents = None if member_root is None else named_contents(member_root, head, tracked, stimulus, cache)
        for first, claim in page_claims:
            reason = unanchored(claim, contents)
            if reason is not None:
                line = first_line + first
                page_findings.append((line, f"{path}:{line}: observation-unanchored: {reason}"))
        for first, unmapped in page_overclaims:
            line = first_line + first
            page_findings.append((line, f"{path}:{line}: observation-overclaim: says Together they cover {unmapped}, "
                                        "which no coverage map names; write Linked requirements, not verified here"))
        findings.extend(finding for _, finding in sorted(page_findings, key=lambda found: found[0]))
    for note in notes:
        print(f"NOTE: {note}")
    for finding in findings:
        print(f"FINDING: {finding}")
    scope = f"{items} test-case items on {pages} pages of wiki commit {commit[:12]}"
    against = f"member commit {evidence.commit[:12]} ({', '.join(evidence.platforms)})"
    if findings:
        print(f"FAIL: {len(findings)} findings in {scope} against {against}")
        return 1
    print(f"PASS: {scope} pass at {against}")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    evidence = commands.add_parser("evidence", help="write execution evidence from a test runner's report")
    evidence.add_argument("--format", choices=FORMATS, default="jest-json",
                          help="report format: jest-json (default) or pytest-junit (pytest --junitxml)")
    evidence.add_argument("--report", required=True,
                          help="Jest-compatible JSON report, such as vitest run --reporter=json --outputFile=<file>")
    evidence.add_argument("--platform", required=True, help="platform the tests ran on, for example linux-x64")
    evidence.add_argument("--root", default=".", help="checkout that ran the tests (default: the current directory)")
    evidence.add_argument("--output", required=True, help="evidence file to write")
    generate = commands.add_parser("generate", help="write the test-case items of the wiki's test pages")
    generate.add_argument("--wiki", required=True, help="wiki working tree")
    generate.add_argument("--evidence", action="append", required=True, help="evidence file; repeat once per platform")
    generate.add_argument("--coverage-map", help="JSON object {test page ID: [requirement IDs its items verify]}, written by a person; "
                          "each ID must be linked by the page's requirements. Without it no page claims coverage")
    generate.add_argument("--dry-run", action="store_true", help="write nothing; exit 1 when a page would change")
    generate.add_argument("ids", nargs="*", help="test page IDs (default: every test page)")
    stimulus = commands.add_parser("stimulus", help="write a repeatable stimulus from the requirements' test artifacts")
    stimulus.add_argument("--wiki", required=True, help="wiki working tree")
    stimulus.add_argument("--evidence", action="append", required=True, help="evidence file; repeat once per platform")
    stimulus.add_argument("--command", dest="last_step", required=True, help="last step, holding {files}, for example 'python -m pytest {files}'")
    stimulus.add_argument("--step", action="append", default=[], help="an earlier step, in order; repeat as needed")
    stimulus.add_argument("--member", default="", help="the member's name, shown with the member commit")
    stimulus.add_argument("--artifact-section", default="TestArtifact", help="requirement section naming test files")
    stimulus.add_argument("--dry-run", action="store_true", help="write nothing; exit 1 when a page would change")
    stimulus.add_argument("ids", nargs="*", help="test page IDs (default: every test page)")
    check = commands.add_parser("check", help="report observation findings for the test pages of one wiki commit")
    check.add_argument("--wiki", required=True, help="wiki repository")
    check.add_argument("--commit", default="HEAD", help="wiki commit to check (default: HEAD)")
    check.add_argument("--member-commit", required=True, help="the member commit that the wiki's test pages describe")
    check.add_argument("--evidence", action="append", required=True, help="evidence file; repeat once per platform")
    check.add_argument("--coverage-map", help="the JSON object that generate read; a Together they cover sentence is "
                                              "reported unless every ID it names is mapped for the page")
    check.add_argument("--anchored", action="store_true",
                       help="also report every claim of a required observation that is not anchored (off by default)")
    check.add_argument("--member-root", help="with --anchored: checkout of the member at the evidence commit; without it no quote "
                                             "can be verified, so every claim that is not a test-case item is unanchored")
    check.add_argument("ids", nargs="*", help="test page IDs (default: every test page)")
    options = parser.parse_args(argv)
    handlers = {"evidence": command_evidence, "generate": command_generate, "stimulus": command_stimulus,
                "check": command_check}
    try:
        return handlers[options.command](options)
    except (Refused, OSError, UnicodeDecodeError, TypeError, ValueError, KeyError, LookupError) as error:
        sys.stderr.write(f"UNKNOWN: {error}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
