"""Leak scan for anything that would become visible to a public audience.

Runs twice: in the submission tool before anything leaves the requester's
machine, and in the gate's preflight before the reviewer summary is
published (REQ-095). Findings name the field and the pattern class only,
never the matched text.

Organization-specific identifiers (private repository or host names) are
supplied at run time through `APPROVAL_GATE_PRIVATE_PATTERNS`, one regular
expression per line, so the patterns themselves never appear in public
engine code or logs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping

BUILTIN_PATTERNS: "tuple[tuple[str, str], ...]" = (
    ("github-token", r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{22,})"),
    ("gitlab-token", r"\bglpat-[A-Za-z0-9_-]{20,}"),
    ("aws-access-key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("slack-token", r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    ("private-key", r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    ("jwt", r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    ("credential-assignment",
     r"(?i)\b(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*['\"]?(?=[^\s'\"]*\d)[^\s'\"]{12,}"),
    ("local-path", r"(?:/home/[A-Za-z0-9._-]+|/Users/[A-Za-z0-9._-]+|\b[A-Za-z]:\\Users\\[^\\\s]+)"),
)


@dataclass(frozen=True)
class Finding:
    field: str
    pattern: str

    def describe(self) -> str:
        return f"{self.field}: matched {self.pattern}"


def compile_patterns(private_patterns: str = "") -> "list[tuple[str, re.Pattern]]":
    compiled = [(name, re.compile(expr)) for name, expr in BUILTIN_PATTERNS]
    for index, line in enumerate((p.strip() for p in private_patterns.splitlines()), start=1):
        if not line or line.startswith("#"):
            continue
        try:
            compiled.append((f"private-identifier-{index}", re.compile(line, re.IGNORECASE)))
        except re.error:
            # An unusable private pattern must not silently disable the scan.
            compiled.append((f"private-identifier-{index}", re.compile(re.escape(line), re.IGNORECASE)))
    return compiled


def readable_text(data: bytes) -> str:
    """Every plausible text reading of `data`, so no file encoding hides a secret from the scan.

    Invalid UTF-8 (cp1252, binary) keeps its ASCII runs; NUL bytes suggest UTF-16
    or UTF-32, such as Windows PowerShell 5.1 `Out-File` output, so those readings
    are scanned too.
    """
    readings = [data.decode("utf-8", errors="replace")]
    if b"\x00" in data:
        readings += [data.decode(encoding, errors="replace")
                     for encoding in ("utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be")]
    return "\n".join(readings)


def scan(fields: "Mapping[str, str] | Iterable[tuple[str, str]]", private_patterns: str = "") -> "list[Finding]":
    items = fields.items() if isinstance(fields, Mapping) else fields
    patterns = compile_patterns(private_patterns)
    findings = []
    for field, text in items:
        if not text:
            continue
        for name, pattern in patterns:
            if pattern.search(text):
                findings.append(Finding(field=field, pattern=name))
    return findings
