"""Preflight: everything that must pass before a reviewer is asked (REQ-094, REQ-095).

The same offline checks run in the submission tool before dispatch, so an
invalid or leaking request never leaves the requester's machine; the online
checks run again in the gate's preflight job, which publishes the reviewer
summary only when the leak scan is clean.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from . import leakscan
from .checks import check_content, check_precondition, is_eligible_reviewer, read_environment
from .github import GitHubClient, GitHubError
from .markdown import cell as _cell, code as _code, fenced as _fenced, html as _html
from .model import REQUEST_ID_RE, Request, RequestError, format_time, parse_request
from .registry import ActionClass

CLOCK_SKEW = timedelta(minutes=10)
LISTING_LIMIT = 50
SCAN_BYTES = 8 * 1024 * 1024
INLINE_FILES = 5
INLINE_BYTES = 4096


@dataclass
class Check:
    id: str
    ok: bool
    detail: str


@dataclass
class StagedFile:
    path: str
    size: int
    text: Optional[str] = None


@dataclass
class PreflightReport:
    request: Optional[Request] = None
    action_class: Optional[ActionClass] = None
    checks: "list[Check]" = field(default_factory=list)
    staged: "list[StagedFile]" = field(default_factory=list)
    staged_truncated: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(check.ok for check in self.checks)

    @property
    def leak_blocked(self) -> bool:
        return any(check.id.startswith("leak-scan") and not check.ok for check in self.checks)

    @property
    def staged_scanned(self) -> bool:
        """True only when the staged listing passed its own leak scan; the summary shows nothing staged otherwise."""
        return any(check.id == "leak-scan-content" and check.ok for check in self.checks) and not self.leak_blocked

    def add(self, check_id: str, ok: bool, detail: str) -> None:
        self.checks.append(Check(check_id, ok, detail))

    def failures(self) -> "list[Check]":
        return [check for check in self.checks if not check.ok]


def offline_checks(
    raw_request: "str | dict",
    *,
    registry: "dict[str, ActionClass]",
    self_repository: str,
    default_branch: Optional[str],
    now: datetime,
    private_patterns: str = "",
    inputs: Optional["dict[str, str]"] = None,
) -> PreflightReport:
    report = PreflightReport()
    raw_text = raw_request if isinstance(raw_request, str) else json.dumps(raw_request, default=str)
    try:
        request = parse_request(raw_request)
    except RequestError as exc:
        report.add("request-schema", False, "; ".join(exc.problems))
        findings = leakscan.scan({"request": raw_text}, private_patterns)
        report.add("leak-scan", not findings, _leak_detail(findings))
        return report
    report.request = request
    report.add("request-schema", True, f"valid request {request.id}")

    if inputs is not None:
        expected = {"request-id": request.id, "action-class": request.action_class,
                    "tracking-issue": str(request.tracking_issue), "expires-at": format_time(request.expires_at)}
        mismatched = [name for name, value in expected.items() if inputs.get(name) != value]
        report.add("inputs-match", not mismatched,
                   "run inputs match the request" if not mismatched
                   else f"run inputs differ from the request: {', '.join(mismatched)}")

    action_class = registry.get(request.action_class)
    if action_class is None:
        report.add("action-class", False, f"unknown action class {request.action_class!r}; "
                   f"known: {', '.join(sorted(registry))}")
    else:
        report.action_class = action_class
        report.add("action-class", True, f"{action_class.name} (environment {action_class.environment})")
        allowed = action_class.target_allowed(request.target_repository, request.target_ref,
                                              self_repository=self_repository, default_branch=default_branch)
        report.add("target", allowed,
                   f"{request.target_repository} {request.target_ref} "
                   + ("matches" if allowed else "is outside") + f" the {action_class.name} target pattern")
        content_ok = request.content.kind in action_class.content_kinds
        if content_ok and request.content.kind == "sha256" and not request.content.ref:
            content_ok = action_class.allow_digest_only
        report.add("content-kind", content_ok,
                   f"{request.content.kind} content" + (" accepted" if content_ok else " is not allowed for this class"))
        precondition_ok = request.precondition.kind in action_class.precondition_kinds
        report.add("precondition-kind", precondition_ok,
                   f"{request.precondition.kind} precondition" + (" accepted" if precondition_ok else " is not allowed for this class"))
        lifetime = request.expires_at - request.requested_at
        expiry_problems = []
        if request.requested_at > now + CLOCK_SKEW:
            expiry_problems.append("requested_at is in the future")
        if request.expires_at <= now:
            expiry_problems.append("the request has already expired")
        if lifetime > timedelta(hours=action_class.max_expiry_hours):
            expiry_problems.append(f"lifetime exceeds the class maximum of {action_class.max_expiry_hours} hours")
        report.add("expiry", not expiry_problems,
                   "; ".join(expiry_problems) or f"expires {format_time(request.expires_at)}")

    fields = dict(request.free_text())
    fields.update(request.identifying_fields())
    findings = leakscan.scan(fields, private_patterns)
    report.add("leak-scan", not findings, _leak_detail(findings))
    return report


def online_checks(
    report: PreflightReport,
    gh: GitHubClient,
    *,
    gate_repository: str,
    run_actor: Optional[str],
    run_head_branch: Optional[str],
    default_branch: Optional[str],
    private_patterns: str = "",
    probe_secret_visible: Optional[bool] = None,
) -> PreflightReport:
    """Checks needing GitHub; skipped unless every offline check passed."""
    request, action_class = report.request, report.action_class
    if request is None or action_class is None or not report.ok:
        return report
    if probe_secret_visible is not None:
        # The gate passes the environment's probe secret to this unprotected job too;
        # seeing it here means it is also a repository or organization secret (REQ-098).
        report.add("secret-isolation", not probe_secret_visible,
                   "the approval environment's probe secret is visible before approval; it must be an environment secret only"
                   if probe_secret_visible else "the approval environment's secrets are not visible before approval")
    if run_actor is not None:
        same = run_actor.lower() == request.requester.lower()
        report.add("requester", same, f"run started by {run_actor}" + ("" if same else f", but the request names {request.requester}"))
    if run_head_branch is not None:
        trusted = default_branch is not None and run_head_branch == default_branch
        report.add("workflow-ref", trusted,
                   f"gate workflow runs from {run_head_branch}" + ("" if trusted else f"; only {default_branch} is trusted"))
    protection = read_environment(gh, gate_repository, action_class.environment)
    problems = protection.problems()
    report.add("environment", not problems,
               "; ".join(message for _, message in problems) or f"{action_class.environment} requires review, prevents self-review, and disallows bypass")
    if protection.exists and protection.readable:
        eligible = is_eligible_reviewer(gh, protection, request.requester)
        # The submission tool refuses when membership is unknown; here an unreadable
        # team is not fatal because prevent-self-review and verify still block self-approval.
        report.add("requester-not-reviewer", eligible is not True,
                   {True: f"{request.requester} is an eligible reviewer and could approve its own request",
                    None: f"team membership of {request.requester} is not readable here; "
                          "prevent-self-review and the verify step still refuse a self-approval",
                    False: f"{request.requester} is not an eligible reviewer"}[eligible])
    precondition = check_precondition(gh, request.precondition)
    report.add("precondition", precondition.ok, precondition.detail)
    content = check_content(gh, request, gate_repository=gate_repository, allow_digest_only=action_class.allow_digest_only)
    report.add("content", content.ok, content.detail)
    if content.ok and request.content.kind == "commit" and request.content.ref:
        _list_staged(report, gh, gate_repository, request.content.value, private_patterns)
    elif content.ok and content.data is not None:
        findings = leakscan.scan([("staged file 1", leakscan.readable_text(content.data))], private_patterns)
        report.add("leak-scan-content", not findings, _leak_detail(findings))
    return report


def _list_staged(report: PreflightReport, gh: GitHubClient, repository: str, sha: str, private_patterns: str) -> None:
    """List and leak-scan every staged file; refuse staged content too large to scan whole.

    Paths and previews reach the report only after the scan, so an API error
    part-way through can never leave unscanned content for the summary.
    """
    contents: list = []
    previews: "dict[int, str]" = {}
    try:
        tree = gh.get(f"/repos/{repository}/git/trees/{sha}", recursive="1")
        entries = tree.get("tree", [])
        blobs = [entry for entry in entries if entry.get("type") == "blob"]
        others = [entry for entry in entries if entry.get("type") not in ("blob", "tree")]
        total = sum(int(entry.get("size") or 0) for entry in blobs)
        truncated = bool(tree.get("truncated")) or len(blobs) > LISTING_LIMIT
        listed = [StagedFile(path=entry["path"], size=int(entry.get("size") or 0)) for entry in blobs[:LISTING_LIMIT]]
        unscannable = []
        if truncated:
            unscannable.append(f"more than {LISTING_LIMIT} files")
        if total > SCAN_BYTES:
            unscannable.append(f"{total} bytes, over the {SCAN_BYTES}-byte limit")
        if others:
            unscannable.append(f"{len(others)} submodule or other non-file entr{'y' if len(others) == 1 else 'ies'}")
        if not unscannable:
            for index, (staged, entry) in enumerate(zip(listed, blobs)):
                data = base64.b64decode(gh.get(f"/repos/{repository}/git/blobs/{entry['sha']}").get("content", ""))
                contents.append((f"staged file {index + 1}", leakscan.readable_text(data)))
                if len(previews) < INLINE_FILES and staged.size <= INLINE_BYTES:
                    try:
                        previews[index] = data.decode("utf-8")
                    except UnicodeDecodeError:
                        pass
    except GitHubError as exc:
        report.add("staged-listing", False, f"staged content unreadable: {exc.api_message} ({exc.status})")
        return
    if unscannable:
        # Nothing unscanned was fetched, so only the scanned paths can reach the summary.
        report.add("staged-scannable", False, "the gate scans all staged content or refuses it: " + "; ".join(unscannable))
    # Field labels are positional so a finding never repeats a staged path.
    fields = [(f"staged path {i}", f.path) for i, f in enumerate(listed, start=1)] + contents
    findings = leakscan.scan(fields, private_patterns)
    report.add("leak-scan-content", not findings, _leak_detail(findings))
    if findings:
        return
    for index, text in previews.items():
        listed[index].text = text
    report.staged, report.staged_truncated = listed, truncated


def _leak_detail(findings: "list[leakscan.Finding]") -> str:
    if not findings:
        return "no secret-shaped strings, local paths, or private identifiers"
    return "blocked: " + "; ".join(finding.describe() for finding in findings)


def render_summary(
    report: PreflightReport,
    *,
    gate_repository: str,
    server_url: str = "https://github.com",
    request_id_input: str = "",
) -> str:
    """Markdown for the run summary: the whole decision before the gate (REQ-095)."""
    request, action_class = report.request, report.action_class
    withheld = report.leak_blocked
    if withheld:
        title = "withheld"
    elif request is not None:
        title = request.id
    else:
        title = request_id_input if REQUEST_ID_RE.match(request_id_input or "") else "invalid request"
    lines = [f"## Approval request `{title}`", ""]
    if report.ok:
        lines += ["> [!IMPORTANT]",
                  "> Review this request, then use **Review deployments** on this run to approve or reject it.",
                  "> Please give a reason when you reject.", ""]
    else:
        lines += ["> [!CAUTION]", "> Preflight failed. No reviewer will be asked, and nothing will be written.", ""]
    if withheld:
        lines += ["_Request details withheld: the leak scan matched. Remove the flagged text and submit a new request._", ""]
    elif request is not None:
        lines += ["| Field | Value |", "| --- | --- |"]
        rows = [
            ("Request digest", f"`{request.digest()}`"),
            ("Action class", f"`{request.action_class}`" + (f" ({_cell(action_class.description)})" if action_class and action_class.description else "")),
            ("Approval environment", f"`{action_class.environment}`" if action_class else "unknown"),
            ("Target", f"`{request.target_repository}` `{request.target_ref}`"),
            ("Content", f"`{request.content.label()}`" + (f" staged on `{request.content.ref}`" if request.content.ref else "")),
            ("Inspect", _inspection_link(request, gate_repository, server_url)),
            ("Precondition", request.precondition.label()),
            ("Requester", f"`{request.requester}`"),
            ("Requested", format_time(request.requested_at)),
            ("Expires", f"**{format_time(request.expires_at)}**"),
            ("Tracking issue", f"[#{request.tracking_issue}]({server_url}/{gate_repository}/issues/{request.tracking_issue})"),
        ]
        lines += [f"| {name} | {value} |" for name, value in rows]
        lines.append("")
        for heading, text in (("Rationale", request.rationale), ("Expected effect", request.expected_effect),
                              ("Rollback", request.rollback)):
            lines += [f"### {heading}", "", *_fenced(text), ""]
        if report.staged and report.staged_scanned:
            lines += [f"### Staged content ({len(report.staged)} file{'s' if len(report.staged) != 1 else ''}"
                      + (", listing truncated" if report.staged_truncated else "") + ")", ""]
            lines += [f"- {_code(f.path)} ({f.size} bytes)" for f in report.staged]
            lines.append("")
            for staged in report.staged:
                if staged.text is not None:
                    lines += [f"<details><summary><code>{_html(staged.path)}</code></summary>", "",
                              *_fenced(staged.text), "", "</details>", ""]
    lines += ["### Preflight checks", "", "| Check | Result | Detail |", "| --- | --- | --- |"]
    for check in report.checks:
        # Check details can echo request values, so only leak-scan findings survive a match.
        detail = check.detail if not withheld or check.id.startswith("leak-scan") else "withheld"
        lines.append(f"| `{check.id}` | {'pass' if check.ok else '**FAIL**'} | {_cell(_html(detail))} |")
    lines.append("")
    return "\n".join(lines)


def _inspection_link(request: Request, gate_repository: str, server_url: str) -> str:
    content = request.content
    if content.kind == "commit" and content.ref:
        return f"[staged commit]({server_url}/{gate_repository}/tree/{content.value})"
    if content.kind == "commit":
        base = request.precondition.sha if request.precondition.kind == "ref-sha" else None
        if base and request.precondition.repository.lower() == request.target_repository.lower():
            return f"[diff against the target]({server_url}/{request.target_repository}/compare/{base}...{content.value})"
        return f"[commit]({server_url}/{request.target_repository}/commit/{content.value})"
    if content.ref:
        return f"[staged file]({server_url}/{gate_repository}/blob/{content.ref}/{content.path})"
    return "digest only: the payload stays with the requester"
