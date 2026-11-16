"""Verify an approval before any side effect (REQ-097).

Runs inside the gated job, after GitHub released it and before the executor.
It re-reads everything from GitHub rather than trusting job inputs, and it
stops at the first failed condition with a reason that names it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .checks import check_content, check_precondition, read_environment
from .github import GitHubClient, GitHubError
from .model import Request, format_time
from .registry import ActionClass

# Reason codes, in check order. The record maps each to a decision.
REASONS = (
    "environment-mismatch",
    "re-run",
    "run-unreadable",
    "untrusted-ref",
    "requester-mismatch",
    "environment-unprotected",
    "history-unreadable",
    "rejected",
    "self-approved",
    "no-approval",
    "expired",
    "drift",
    "digest-mismatch",
)


@dataclass
class Review:
    login: str
    state: str
    comment: str

    def to_dict(self) -> dict:
        return {"login": self.login, "state": self.state, "comment": self.comment}


@dataclass
class Verdict:
    ok: bool
    reason: Optional[str]
    detail: str
    reviews: "list[Review]" = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "reason": self.reason, "detail": self.detail,
                "reviews": [review.to_dict() for review in self.reviews]}


def environment_reviews(approvals: list, environment: str) -> "list[Review]":
    """Reviews recorded for `environment` in the run's review history."""
    reviews = []
    for entry in approvals or []:
        names = {(env.get("name") or "").lower() for env in entry.get("environments") or []}
        if environment.lower() in names:
            reviews.append(Review(login=(entry.get("user") or {}).get("login") or "",
                                  state=entry.get("state") or "", comment=entry.get("comment") or ""))
    return reviews


def read_reviews(gh: GitHubClient, repository: str, run_id: int, environment: str) -> "list[Review]":
    return environment_reviews(gh.get(f"/repos/{repository}/actions/runs/{run_id}/approvals"), environment)


def verify(
    gh: GitHubClient,
    request: Request,
    action_class: ActionClass,
    *,
    gate_repository: str,
    run_id: int,
    run_attempt: int,
    job_environment: str,
    default_branch: Optional[str],
    now: datetime,
) -> Verdict:
    def refuse(reason: str, detail: str, reviews: "Optional[list[Review]]" = None) -> Verdict:
        return Verdict(False, reason, detail, reviews or [])

    if job_environment != action_class.environment:
        return refuse("environment-mismatch",
                      f"job ran in environment {job_environment!r}, but class {action_class.name} requires {action_class.environment!r}")
    if run_attempt != 1:
        return refuse("re-run", f"run attempt {run_attempt}: an approval releases only the first attempt; submit a new request")
    try:
        run = gh.get(f"/repos/{gate_repository}/actions/runs/{run_id}")
    except GitHubError as exc:
        return refuse("run-unreadable", f"run {run_id} unreadable: {exc.api_message} ({exc.status})")
    if run.get("run_attempt", 1) != 1:
        return refuse("re-run", f"run attempt {run.get('run_attempt')}: an approval releases only the first attempt")
    head_branch = run.get("head_branch")
    if run.get("event") != "workflow_dispatch" or not default_branch or head_branch != default_branch:
        return refuse("untrusted-ref",
                      f"run came from {run.get('event')} on {head_branch}; only workflow_dispatch on {default_branch} is trusted")
    actor = (run.get("triggering_actor") or run.get("actor") or {}).get("login") or ""
    if actor.lower() != request.requester.lower():
        return refuse("requester-mismatch", f"run triggered by {actor or 'unknown'}, but the request names {request.requester}")

    problems = read_environment(gh, gate_repository, action_class.environment).problems()
    if problems:
        return refuse("environment-unprotected", "; ".join(message for _, message in problems))

    try:
        reviews = read_reviews(gh, gate_repository, run_id, action_class.environment)
    except GitHubError as exc:
        return refuse("history-unreadable", f"review history unreadable: {exc.api_message} ({exc.status})")
    rejected = [review for review in reviews if review.state == "rejected"]
    if rejected:
        return refuse("rejected", f"rejected by {', '.join(r.login for r in rejected)}", reviews)
    approved = [review for review in reviews if review.state == "approved"]
    by_requester = [review for review in approved if review.login.lower() == request.requester.lower()]
    if by_requester:
        return refuse("self-approved", f"approved by the requester {request.requester}", reviews)
    if not approved:
        return refuse("no-approval", f"no approval for environment {action_class.environment} in this run's review history", reviews)
    if now >= request.expires_at:
        return refuse("expired", f"request expired at {format_time(request.expires_at)}", reviews)

    precondition = check_precondition(gh, request.precondition)
    if not precondition.ok:
        return refuse("drift", precondition.detail, reviews)
    content = check_content(gh, request, gate_repository=gate_repository, allow_digest_only=action_class.allow_digest_only)
    if not content.ok:
        return refuse("digest-mismatch", content.detail, reviews)
    return Verdict(True, None, f"approved by {', '.join(r.login for r in approved)}; precondition and content unchanged", reviews)
