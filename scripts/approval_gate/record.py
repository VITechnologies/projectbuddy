"""Decision records on the tracking issue (REQ-099).

Every gate run ends with one comment on the request's tracking issue, written
by the record job even when the gated job was rejected, cancelled, or failed.
The comment holds a human table and the same record as embedded JSON, so the
decision stays readable after the run's logs are deleted (TEST-101).
"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from . import RECORD_SCHEMA, TOOL_VERSION, leakscan
from .github import GitHubClient
from .markdown import cell as _cell, fenced, html as _html
from .model import Request, format_time
from .registry import ActionClass
from .verify import Review

DECISIONS = ("approved", "rejected", "expired", "failed")
RECORD_AUTHOR = "github-actions[bot]"
DATA_RE = re.compile(r"<!-- approval-gate-record-data: ([A-Za-z0-9+/=]+) -->")
EXECUTE_JOB_NAME = "Execute approved request"
WITHHELD_ID = "withheld"

# Refusal reasons after a human approved: the decision stays "approved".
APPROVED_REFUSALS = ("drift", "digest-mismatch")


def marker(request_id: str, run_id: int, attempt: int) -> str:
    return f"<!-- approval-gate-record: {request_id} run {run_id} attempt {attempt} -->"


@dataclass
class RunContext:
    repository: str
    run_id: int
    run_attempt: int
    server_url: str = "https://github.com"

    @property
    def url(self) -> str:
        return f"{self.server_url}/{self.repository}/actions/runs/{self.run_id}"


def decide(
    *,
    request: Optional[Request],
    request_id: str,
    preflight_ok: bool,
    execute_result: str,
    verdict: Optional[dict],
    reviews: "list[Review]",
    now: datetime,
) -> "tuple[str, str, str]":
    """(decision, outcome, detail) from everything the record job can observe."""
    if verdict is not None:
        reason = verdict.get("reason")
        execution = verdict.get("execution") or {}
        if verdict.get("ok"):
            if execution.get("error"):
                return "failed", "failed:executor-error", str(execution["error"])
            return "approved", "executed", str(verdict.get("detail", ""))
        decision = ("rejected" if reason == "rejected" else "expired" if reason == "expired"
                    else "approved" if reason in APPROVED_REFUSALS else "failed")
        return decision, f"refused:{reason}", str(verdict.get("detail", ""))
    if not preflight_ok or request is None:
        return "failed", "not-run:preflight", f"preflight did not pass for {request_id}; see the run summary"
    rejected = [review for review in reviews if review.state == "rejected"]
    if rejected:
        return "rejected", "not-run", f"rejected by {', '.join(review.login for review in rejected)}"
    if now >= request.expires_at:
        return "expired", "not-run:expired", f"no approval before {format_time(request.expires_at)}"
    if execute_result == "cancelled":
        return "failed", "not-run:withdrawn", "the run was cancelled before a decision"
    return "failed", f"not-run:{execute_result or 'unknown'}", "the gated job ended without a verdict"


def build_record(
    *,
    request: Optional[Request],
    request_id: str,
    action_class: Optional[ActionClass],
    decision: str,
    outcome: str,
    detail: str,
    reviews: "list[Review]",
    run: RunContext,
    now: datetime,
    decided_at: Optional[str],
    execution: Optional[dict],
    private_patterns: str = "",
) -> dict:
    # A request that failed preflight on the leak scan still parses, so scan again
    # before anything request-derived reaches the tracking issue.
    request_fields = {"request_id": request_id}
    if request is not None:
        request_fields.update(request.identifying_fields())
    request_withheld = bool(leakscan.scan(request_fields, private_patterns))
    if request_withheld:
        request, request_id, execution = None, WITHHELD_ID, None
        detail = "request details withheld: they matched the leak scan; see the run summary"
    reviewers = []
    for review in reviews:
        withheld = bool(leakscan.scan({"comment": review.comment}, private_patterns))
        reviewers.append({
            "login": review.login,
            "state": review.state,
            "comment": "" if withheld else review.comment,
            "comment_withheld": withheld,
            "comment_missing": review.state == "rejected" and not review.comment.strip(),
        })
    record: dict = {
        "schema": RECORD_SCHEMA,
        "tool_version": TOOL_VERSION,
        "request_id": request_id,
        "decision": decision,
        "outcome": outcome,
        "detail": detail,
        "reviewers": reviewers,
        "run": {"repository": run.repository, "id": run.run_id, "attempt": run.run_attempt, "url": run.url},
        "decided_at": decided_at,
        "recorded_at": format_time(now),
        "execution": execution,
        "request_withheld": request_withheld,
    }
    if request is not None:
        record.update({
            "request_digest": request.digest(),
            "action_class": request.action_class,
            "target": {"repository": request.target_repository, "ref": request.target_ref},
            "content": request.content.to_dict(),
            "requester": request.requester,
            "tracking_issue": request.tracking_issue,
            "requested_at": format_time(request.requested_at),
            "expires_at": format_time(request.expires_at),
        })
    if action_class is not None:
        record["environment"] = action_class.environment
    return record


def render_comment(record: dict) -> str:
    request_id = record["request_id"]
    lines = [marker(request_id, record["run"]["id"], record["run"]["attempt"]),
             f"### Approval request `{request_id}`: **{record['decision']}**", "",
             "| Field | Value |", "| --- | --- |",
             f"| Decision | **{record['decision']}** |",
             f"| Outcome | `{record['outcome']}` |",
             f"| Detail | {_cell(_html(record.get('detail') or ''))} |"]
    if "request_digest" in record:
        target = record["target"]
        lines += [f"| Request digest | `{record['request_digest']}` |",
                  f"| Action class | `{record['action_class']}`"
                  + (f" (environment `{record['environment']}`)" if record.get("environment") else "") + " |",
                  f"| Target | `{target['repository']}` `{target['ref']}` |",
                  f"| Content | `{record['content']['kind']} {record['content']['value']}` |",
                  f"| Requester | `{record['requester']}` |",
                  f"| Requested | {record['requested_at']} |",
                  f"| Expires | {record['expires_at']} |"]
    lines += [f"| Decided | {record.get('decided_at') or 'unknown'} |",
              f"| Recorded | {record['recorded_at']} |",
              f"| Run | [{record['run']['id']} attempt {record['run']['attempt']}]({record['run']['url']}) |"]
    execution = record.get("execution")
    if execution and "writes" in execution:
        lines.append(f"| Writes | {execution['writes']} |")
    lines.append("")
    if record["reviewers"]:
        lines += ["#### Reviews", ""]
        for reviewer in record["reviewers"]:
            lines.append(f"- `{reviewer['login']}` **{reviewer['state']}**")
            if reviewer["comment_withheld"]:
                lines.append("  - _Comment withheld: it matched the leak scan._")
            elif reviewer["comment_missing"]:
                lines.append("  - **No reason given.** Please add one as a reply.")
            elif reviewer["comment"]:
                lines += ["", *fenced(reviewer["comment"], indent="  ")]
        lines.append("")
    else:
        lines += ["_No review recorded for this run._", ""]
    encoded = base64.b64encode(json.dumps(record, sort_keys=True).encode("utf-8")).decode("ascii")
    lines.append(f"<!-- approval-gate-record-data: {encoded} -->")
    return "\n".join(lines) + "\n"


def parse_comment(body: str) -> Optional[dict]:
    # The payload is always the final line, so text quoted above it (a reviewer
    # comment or a request value) can never stand in for the real record.
    lines = (body or "").rstrip().splitlines()
    match = DATA_RE.fullmatch(lines[-1].strip()) if lines else None
    if not match:
        return None
    try:
        record = json.loads(base64.b64decode(match.group(1)).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return record if isinstance(record, dict) and record.get("schema") == RECORD_SCHEMA else None


def find_records(gh: GitHubClient, repository: str, issue: int, *, request_id: Optional[str] = None,
                 run_id: Optional[int] = None, attempt: Optional[int] = None,
                 author: str = RECORD_AUTHOR) -> "list[tuple[dict, dict]]":
    """(comment, record) pairs written by the gate; comments by anyone else are ignored."""
    found = []
    for comment in gh.paginate(f"/repos/{repository}/issues/{issue}/comments"):
        if ((comment.get("user") or {}).get("login") or "").lower() != author.lower():
            continue
        record = parse_comment(comment.get("body") or "")
        if record is None:
            continue
        if request_id is not None and record.get("request_id") != request_id:
            continue
        if run_id is not None and record.get("run", {}).get("id") != run_id:
            continue
        if attempt is not None and record.get("run", {}).get("attempt") != attempt:
            continue
        found.append((comment, record))
    return found


def upsert(gh: GitHubClient, repository: str, issue: int, record: dict, *, author: str = RECORD_AUTHOR) -> dict:
    """Post the record, or edit this run attempt's earlier record so a rerun record job stays idempotent."""
    body = render_comment(record)
    existing = find_records(gh, repository, issue, request_id=record["request_id"], run_id=record["run"]["id"],
                            attempt=record["run"]["attempt"], author=author)
    if existing:
        comment = existing[0][0]
        return gh.patch(f"/repos/{repository}/issues/comments/{comment['id']}", {"body": body})
    return gh.post(f"/repos/{repository}/issues/{issue}/comments", {"body": body})


def decided_at(jobs: list, decision: str) -> Optional[str]:
    """When the gated job was released (approved) or ended (otherwise); approvals carry no timestamp."""
    for job in jobs or []:
        if (job.get("name") or "").endswith(EXECUTE_JOB_NAME):
            stamp = job.get("started_at") if decision == "approved" else job.get("completed_at")
            return stamp or None
    return None
