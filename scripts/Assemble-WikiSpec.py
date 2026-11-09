#!/usr/bin/env python3
"""Assemble-WikiSpec.py: rebuild the governed specification hubs from the ProjectBuddy wiki.

The wiki may keep each SYS-REQ, REQ, and TEST ID and each Architecture
Packet section on its own page, so Obsidian's graph and backlinks show the
trace relationships. Hub pages then hold invisible markers that list the
pages forming each table or section tree:

    <!-- table: | ID | Requirement | Rationale | Fit Criterion | Verification | -->
    - [[REQ-001]]
    <!-- /table -->

    <!-- sections -->
    - [[Context-View]]
    <!-- /sections -->

This tool rebuilds the monolithic files the standards scan reads and fails
closed on any structural problem, so no page content can silently drop out
of the governed scan input. In the split layout every page must be a hub, a
section reachable from a hub, an ID page listed in a table, or carry a
page-kind marker directly under its title: `stub` or `retired` for ID pages
outside the tables, `note` for ungoverned pages such as Home. A wiki without
table or sections markers assembles unchanged.

Usage:
    Assemble-WikiSpec.py --wiki DIR [--emit PAGE=PATH ...] [--strict [--max-page-bytes N]]

On any error nothing is written. Emitted files are staged and replaced only
after every staged write succeeds, so a PATH may be the hub page itself.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

ID_PATTERN = r"(?:SYS-REQ|REQ|TEST)-\d{3}"
ID_NAME = re.compile(rf"{ID_PATTERN}\Z")
ID_MENTION = re.compile(rf"(?<![\w-])({ID_PATTERN})(?!\d)")
ID_H1 = re.compile(rf"# ({ID_PATTERN})(?:: (.+))?\n")
SECTION_HEADING = re.compile(r"^## (.+)\n", re.M)
PAGE_KIND = re.compile(r"<!-- page-kind: ([a-z-]*) -->")
KIND_LINE = re.compile(r"\n<!-- page-kind: ([a-z-]*) -->\n")
ID_KINDS = {"retired", "stub"}
NOTE_KIND = "note"
UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")
TABLE_BLOCK = re.compile(
    r"^<!-- table: (\|.*\|) -->\n((?:- \[\[[^\]\n]+\]\]\n)*)<!-- /table -->$", re.M
)
SECTIONS_BLOCK = re.compile(
    r"\n<!-- sections -->\n((?:- \[\[[^\]\n]+\]\]\n)*)<!-- /sections -->\n\Z"
)
INDEX_ITEM = re.compile(r"- \[\[([^\]\n]+)\]\]\n")
MARKER_TEXT = re.compile(r"<!-- /?(?:table|sections)\b")
WIKILINK = re.compile(r"\[\[([^\]\n]*)\]\]")
INLINE_CODE = re.compile(r"`[^`\n]*`")
FENCE = re.compile(r"^(```|~~~)")
ATX_HEADING = re.compile(r"^#{1,6} ")
TITLE_COLUMN = "Requirement (short)"
MAX_DEPTH = 6


class SpecError(Exception):
    pass


@dataclass
class Page:
    name: str
    rel: str
    text: str
    is_id: bool = False
    title: str | None = None
    kind: str | None = None
    sections: dict[str, str] = field(default_factory=dict)


def github_slug(text: str) -> str:
    slug = re.sub(r"[^\w\- ]", "", text.strip().lower())
    return slug.replace(" ", "-")


def segments(text: str):
    """Yield (is_code, chunk) pairs; fenced blocks and inline code are code."""
    in_fence = False
    for line in text.splitlines(keepends=True):
        if FENCE.match(line):
            in_fence = not in_fence
            yield True, line
            continue
        if in_fence:
            yield True, line
            continue
        pos = 0
        for match in INLINE_CODE.finditer(line):
            if match.start() > pos:
                yield False, line[pos : match.start()]
            yield True, match.group(0)
            pos = match.end()
        if pos < len(line):
            yield False, line[pos:]


def map_prose(text: str, func) -> str:
    return "".join(chunk if code else func(chunk) for code, chunk in segments(text))


def map_unlinked(text: str, func) -> str:
    """Apply func to prose outside code spans and outside existing wikilinks."""

    def outside_links(chunk: str) -> str:
        pieces = WIKILINK.split(chunk)
        for index in range(0, len(pieces), 2):
            pieces[index] = func(pieces[index])
        for index in range(1, len(pieces), 2):
            pieces[index] = f"[[{pieces[index]}]]"
        return "".join(pieces)

    return map_prose(text, outside_links)


def prose_links(text: str):
    for code, chunk in segments(text):
        if not code:
            yield from (m.group(1) for m in WIKILINK.finditer(chunk))


def plain_mentions(text: str) -> list[str]:
    found: list[str] = []
    map_unlinked(text, lambda chunk: found.extend(ID_MENTION.findall(chunk)) or chunk)
    return found


def body_headings(text: str) -> list[str]:
    found = []
    in_fence = False
    for line in text.splitlines():
        if FENCE.match(line):
            in_fence = not in_fence
        elif not in_fence and ATX_HEADING.match(line):
            found.append(line)
    return found


class Wiki:
    def __init__(self, root: Path):
        self.root = root
        self.errors: list[str] = []
        self.pages: dict[str, Page] = {}
        self.children: dict[str, list[str]] = {}
        self.parent: dict[str, str] = {}
        self.section_anchor: dict[str, str] = {}
        self.consumed: dict[str, set[str]] = defaultdict(set)
        self.table_rows: dict[str, list[str]] = defaultdict(list)
        self._load()
        self._parse_id_pages()
        self._build_trees()

    def error(self, message: str) -> None:
        self.errors.append(message)

    def _load(self) -> None:
        folded: dict[str, str] = {}
        for path in sorted(self.root.rglob("*.md")):
            rel = path.relative_to(self.root)
            if any(part.startswith(".") for part in rel.parts):
                continue
            name = path.stem
            key = name.casefold()
            if key in folded:
                self.error(f"duplicate page name: {folded[key]} and {rel.as_posix()}")
                continue
            folded[key] = rel.as_posix()
            text = path.read_bytes().decode("utf-8")
            page = Page(name, rel.as_posix(), text)
            self.pages[name] = page
            self._read_kind(page)

    def _read_kind(self, page: Page) -> None:
        markers = PAGE_KIND.findall(page.text)
        if not markers:
            return
        rest = page.text.partition("\n")[2]
        placed = KIND_LINE.match(rest)
        if len(markers) != 1 or not placed:
            self.error(f"{page.rel}: page-kind marker must appear once, directly after the title and a blank line")
            return
        page.kind = placed.group(1)
        allowed = ID_KINDS if ID_NAME.match(page.name) else {NOTE_KIND}
        if page.kind not in allowed:
            self.error(f"{page.rel}: page-kind '{page.kind}' is not allowed here; use one of {sorted(allowed)}")
        if MARKER_TEXT.search(page.text):
            self.error(f"{page.rel}: pages with a page-kind marker may not hold table or sections markers")
        if page.kind in ID_KINDS and SECTION_HEADING.search(rest[placed.end() :]):
            self.error(f"{page.rel}: {page.kind} pages may not hold column sections; remove the marker to govern them")

    def _cell_error(self, value: str | None) -> str | None:
        if not value or "\n" in value or value != value.strip():
            return "must be one nonblank line without edge whitespace"
        if UNESCAPED_PIPE.search(value):
            return "contains an unescaped table pipe"
        return None

    def _parse_id_pages(self) -> None:
        for page in self.pages.values():
            if not ID_NAME.match(page.name):
                continue
            page.is_id = True
            if "\r" in page.text:
                self.error(f"{page.rel}: ID pages must use LF line endings")
                continue
            head = ID_H1.match(page.text)
            if not head or head.group(1) != page.name:
                self.error(f"{page.rel}: first line must be '# {page.name}' or '# {page.name}: <title>'")
                continue
            page.title = head.group(2)
            if page.title is not None and (problem := self._cell_error(page.title)):
                self.error(f"{page.rel}: title {problem}")
            rest = page.text[head.end() :]
            if page.kind:
                continue
            parts = SECTION_HEADING.split(rest)
            if rest.strip() and parts[0] != "\n":
                self.error(f"{page.rel}: only '## <Column>' sections may follow the title")
                continue
            pairs = list(zip(parts[1::2], parts[2::2]))
            if not pairs:
                self.error(f"{page.rel}: no column sections and no page-kind marker")
            for index, (column, body) in enumerate(pairs):
                last = index == len(pairs) - 1
                tail = "\n" if last else "\n\n"
                framed = body.startswith("\n") and body.endswith(tail) and len(body) > len(tail)
                value = body[1 : -len(tail)] if framed else None
                if not framed:
                    self.error(f"{page.rel}: section '{column}' must be one blank line, one nonblank line, then a blank line")
                    continue
                if problem := self._cell_error(value):
                    self.error(f"{page.rel}: section '{column}' {problem}")
                    continue
                if column in page.sections:
                    self.error(f"{page.rel}: duplicate section '{column}'")
                page.sections[column] = value

    def _build_trees(self) -> None:
        for page in self.pages.values():
            if page.is_id:
                continue
            _, _, rest = page.text.partition("\n")
            block = SECTIONS_BLOCK.search(rest)
            if not block:
                continue
            kids = INDEX_ITEM.findall(block.group(1))
            self.children[page.name] = kids
            for kid in kids:
                if kid in self.parent:
                    self.error(f"section page [[{kid}]] is listed by both [[{self.parent[kid]}]] and [[{page.name}]]")
                    continue
                self.parent[kid] = page.name
                child = self.pages.get(kid)
                if child is None:
                    self.error(f"{page.rel}: section index lists missing page [[{kid}]]")
                elif child.is_id:
                    self.error(f"{page.rel}: section index lists ID page [[{kid}]]")
                elif child.kind:
                    self.error(f"{page.rel}: section index lists {child.kind} page [[{kid}]]")
        for name in self.parent:
            seen = {name}
            node = name
            while node in self.parent:
                node = self.parent[node]
                if node in seen:
                    self.error(f"section cycle through [[{name}]]")
                    break
                seen.add(node)
            else:
                child = self.pages.get(name)
                if child:
                    first = child.text.partition("\n")[0]
                    if not first.startswith("# "):
                        self.error(f"{child.rel}: section pages must start with '# <heading>'")
                    self.section_anchor[name] = f"{node}#{github_slug(first[2:])}"

    def roots(self) -> list[str]:
        has_markers = [n for n, p in self.pages.items() if not p.is_id and MARKER_TEXT.search(p.text)]
        return sorted(n for n in has_markers if n not in self.parent)

    def _expand_tables(self, text: str, owner: Page) -> str:
        def replace(match: re.Match) -> str:
            header = match.group(1)
            columns = header[2:-2].split(" | ")
            ids = INDEX_ITEM.findall(match.group(2))
            lines = [header, "| " + " | ".join("---" for _ in columns) + " |"]
            seen: set[str] = set()
            for ident in ids:
                if ident in seen:
                    self.error(f"{owner.rel}: [[{ident}]] is listed twice in table '{header}'")
                    continue
                seen.add(ident)
                page = self.pages.get(ident)
                if page is None or not page.is_id:
                    self.error(f"{owner.rel}: table lists missing ID page [[{ident}]]")
                    continue
                if page.kind:
                    self.error(f"{owner.rel}: table lists {page.kind} page [[{ident}]]")
                    continue
                self.table_rows[ident].append(f"{owner.name}: {header}")
                cells = [ident]
                for column in columns[1:]:
                    if column == TITLE_COLUMN:
                        value = page.title
                    else:
                        value = page.sections.get(column)
                        self.consumed[ident].add(column)
                    if value is None:
                        self.error(f"{page.rel}: missing '{column}' required by {owner.name}")
                        value = ""
                    cells.append(value)
                lines.append("| " + " | ".join(cells) + " |")
            return "\n".join(lines)

        return TABLE_BLOCK.sub(replace, text)

    def _emit(self, name: str, depth: int) -> str:
        page = self.pages[name]
        if depth > MAX_DEPTH:
            self.error(f"{page.rel}: section depth exceeds h{MAX_DEPTH}")
            return ""
        if "\r" in page.text and (depth > 1 or MARKER_TEXT.search(page.text)):
            self.error(f"{page.rel}: split pages must use LF line endings")
            return ""
        first, newline, rest = page.text.partition("\n")
        block = SECTIONS_BLOCK.search(rest)
        body = rest[: block.start()] if block else rest
        if depth > 1:
            for heading in body_headings(body):
                self.error(f"{page.rel}: section pages may not contain headings ({heading!r}); split it into a child page")
            first = "#" * depth + " " + first[2:]
        body = self._expand_tables(body, page)
        if MARKER_TEXT.search(body):
            self.error(f"{page.rel}: malformed table or sections marker")
        out = first + newline + body
        for kid in self.children.get(name, []):
            if kid in self.pages and not self.pages[kid].is_id:
                out += "\n" + self._emit(kid, depth + 1)
        return out

    def _relink(self, target: str) -> str:
        if target in self.pages and self.pages[target].is_id:
            return target
        if target in self.section_anchor:
            return f"[[{self.section_anchor[target]}]]"
        return f"[[{target}]]"

    def assemble(self, name: str) -> str:
        text = self._emit(name, 1)
        return map_prose(text, lambda chunk: WIKILINK.sub(lambda m: self._relink(m.group(1)), chunk))

    def validate(self, emitted: set[str]) -> dict[str, str]:
        roots = self.roots()
        assembled = {name: self.assemble(name) for name in roots}
        for page in self.pages.values():
            if not page.is_id or page.kind or not page.sections:
                continue
            unused = set(page.sections) - self.consumed[page.name]
            if not self.table_rows[page.name]:
                self.error(f"{page.rel}: ID page is in no table and has no page-kind marker")
            elif unused:
                self.error(f"{page.rel}: sections not consumed by any table: {sorted(unused)}")
        if roots:
            for page in self.pages.values():
                if not (page.is_id or page.name in roots or page.name in self.parent or page.kind == NOTE_KIND):
                    self.error(f"{page.rel}: page is in no hub index; list it in a parent's sections or mark it as a note")
            if emitted:
                for name in sorted(set(roots) - emitted):
                    self.error(f"hub [[{name}]] holds governed content but is not emitted")
        return assembled

    def strict_checks(self, max_page_bytes: int | None) -> None:
        names = set(self.pages)
        for page in self.pages.values():
            size = len(page.text.encode("utf-8"))
            if max_page_bytes is not None and size > max_page_bytes:
                self.error(f"{page.rel}: {size} bytes exceeds the {max_page_bytes}-byte page budget")
            for target in prose_links(page.text):
                if "|" in target:
                    self.error(f"{page.rel}: piped link [[{target}]] renders differently in GitHub and Obsidian")
                elif "#" in target:
                    self.error(f"{page.rel}: heading-anchor link [[{target}]]; link the section page instead")
                elif target not in names:
                    self.error(f"{page.rel}: unresolved link [[{target}]]")
            for ident in plain_mentions(page.text):
                if ident != page.name and ident in names:
                    self.error(f"{page.rel}: unlinked mention of {ident}; write [[{ident}]] or put it in code")


def parse_emit(values: list[str]) -> list[tuple[str, Path]]:
    pairs = []
    for value in values:
        name, sep, path = value.partition("=")
        if not sep or not name or not path:
            raise SpecError(f"--emit expects PAGE=PATH, got {value!r}")
        pairs.append((name, Path(path)))
    return pairs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--wiki", type=Path, required=True, help="wiki checkout")
    parser.add_argument("--emit", action="append", default=[], metavar="PAGE=PATH",
                        help="write the assembled hub PAGE to PATH (repeatable)")
    parser.add_argument("--strict", action="store_true",
                        help="also enforce link hygiene for the split layout")
    parser.add_argument("--max-page-bytes", type=int, help="page size budget checked with --strict")
    args = parser.parse_args(argv)

    try:
        targets = parse_emit(args.emit)
    except SpecError as exc:
        parser.error(str(exc))
    wiki = Wiki(args.wiki)
    outputs: dict[Path, str] = {}
    if not wiki.errors:
        assembled = wiki.validate({name for name, _ in targets})
        if args.strict:
            wiki.strict_checks(args.max_page_bytes)
        for name, path in targets:
            page = wiki.pages.get(name)
            if page is None or page.is_id or name in wiki.parent:
                wiki.error(f"--emit {name}: not a hub page")
                continue
            if path.resolve() in outputs:
                wiki.error(f"--emit {name}: output path {path} is used twice")
                continue
            outputs[path.resolve()] = assembled[name] if name in assembled else wiki.assemble(name)
    if wiki.errors:
        for message in sorted(set(wiki.errors)):
            print(f"error: {message}", file=sys.stderr)
        print(f"{len(set(wiki.errors))} error(s); nothing written", file=sys.stderr)
        return 1
    staged = []
    for path, text in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.assembling")
        temp.write_bytes(text.encode("utf-8"))
        staged.append((temp, path, hashlib.sha256(text.encode("utf-8")).hexdigest()))
    for temp, path, digest in staged:
        os.replace(temp, path)
        print(f"wrote {path} sha256={digest}")
    roots = set(wiki.roots())
    kinds = defaultdict(int)
    for page in wiki.pages.values():
        kinds[page.kind or ("id" if page.is_id else "section" if page.name in wiki.parent else "hub" if page.name in roots else "page")] += 1
    stubs = {n for n, p in wiki.pages.items() if p.kind == "stub"}
    cited = {n for n, p in wiki.pages.items() if not p.kind for v in p.sections.values() if set(ID_MENTION.findall(v)) & stubs}
    print(f"ok: {len(wiki.pages)} pages {dict(sorted(kinds.items()))}; {len(cited)} governed ID pages cite stub tests")
    return 0


if __name__ == "__main__":
    sys.exit(main())
