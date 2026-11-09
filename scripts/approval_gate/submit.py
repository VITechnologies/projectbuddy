"""Requester side: stage, submit, follow, withdraw, and sweep requests.

One request is one `workflow_dispatch` run of the adopter's request workflow
(REQ-094). Runs are found again by their title, so every command works from a
request ID alone and a repeated `submit` never creates a second run.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import PurePosixPath
from typing import Callable, Optional

from . import DEFAULT_REGISTRY_PATH, RESULT_SCHEMA, RUN_TITLE_PREFIX, STAGING_BRANCH_PREFIX, TOOL_VERSION, record
from .checks import resolve_ref
from .github import GitHubClient
from .model import CONTENT_PATH_RE, Request, format_time, parse_time
from .registry import ActionClass, RegistryError, default_registry, parse_registry

TITLE_RE = re.compile(
    r"^Approval request (?P<id>[a-z0-9][a-z0-9._-]{2,62}) · (?P<action_class>[a-z][a-z0-9-]{1,40}) "
    r"· #(?P<issue>\d+) · expires (?P<expires>\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)$"
)
EXIT_CODES = {"executed": 0, "rejected": 10, "expired": 11, "failed": 12, "pending": 20}
EXIT_REFUSED = 3
EXIT_INVALID = 2
EXIT_ERROR = 1
UNSTAGEABLE_PREFIX = ".github/workflows/"

Sleep = Callable[[float], None]
Clock = Callable[[], datetime]


class SubmitError(RuntimeError):
    """The tool refused or could not complete; `exit_code` says which."""

    def __init__(self, message: str, exit_code: int = EXIT_ERROR) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def run_title(request: Request) -> str:
    return (f"{RUN_TITLE_PREFIX}{request.id} · {request.action_class} · #{request.tracking_issue} "
            f"· expires {format_time(request.expires_at)}")


def parse_title(title: str) -> Optional[dict]:
    match = TITLE_RE.match(title or "")
    if not match:
        return None
    parsed = match.groupdict()
    parsed["issue"] = int(parsed["issue"])
    return parsed


def dispatch_inputs(request: Request) -> "dict[str, str]":
    return {
        "request-id": request.id,
        "action-class": request.action_class,
        "tracking-issue": str(request.tracking_issue),
        "expires-at": format_time(request.expires_at),
        "request": request.canonical_json(),
    }


def load_remote_registry(gh: GitHubClient, repository: str, ref: str) -> "dict[str, ActionClass]":
    """The registry from the gate repository's default branch, the only trusted source."""
    data = gh.try_get(f"/repos/{repository}/contents/{DEFAULT_REGISTRY_PATH}", ref=ref)
    if data is None:
        return default_registry()
    try:
        return parse_registry(json.loads(base64.b64decode(data.get("content", "")).decode("utf-8")),
                              source=f"{repository}:{DEFAULT_REGISTRY_PATH}")
    except (ValueError, UnicodeDecodeError) as exc:
        raise RegistryError(f"{repository}:{DEFAULT_REGISTRY_PATH}: {exc}") from None


# --- staging -----------------------------------------------------------------

def check_stage_paths(paths: "list[str]") -> "list[str]":
    problems = []
    for path in paths:
        normalized = str(PurePosixPath(path))
        if normalized != path or not CONTENT_PATH_RE.match(path):
            problems.append(f"staged path {path!r} must be a relative path of letters, digits, '.', '_', '-', or '/'")
        elif path.lower().startswith(UNSTAGEABLE_PREFIX):
            problems.append(f"staged path {path!r} is a workflow file; staged refs must never contain runnable workflows")
        elif ".git" in path.lower().split("/"):
            problems.append(f"staged path {path!r} has a '.git' component, which Git checkouts treat as repository metadata")
    if len(set(paths)) != len(paths):
        problems.append("staged paths must be unique")
    return problems


def stage(gh: GitHubClient, repository: str, request_id: str, files: "dict[str, bytes]") -> str:
    """Stage files as an orphan commit on `requests/<id>` (D8) and return its SHA.

    An existing staging ref is reused only when it holds exactly the same tree;
    a different tree under the same request ID is refused, never overwritten.
    """
    problems = check_stage_paths(sorted(files))
    if problems or not files:
        raise SubmitError("; ".join(problems) or "nothing to stage", EXIT_INVALID)
    entries = []
    for path in sorted(files):
        blob = gh.post(f"/repos/{repository}/git/blobs",
                       {"content": base64.b64encode(files[path]).decode("ascii"), "encoding": "base64"})
        entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    tree = gh.post(f"/repos/{repository}/git/trees", {"tree": entries})["sha"]
    branch = f"{STAGING_BRANCH_PREFIX}{request_id}"
    existing = resolve_ref(gh, repository, f"refs/heads/{branch}")
    if existing:
        commit = gh.get(f"/repos/{repository}/git/commits/{existing}")
        if (commit.get("tree") or {}).get("sha") == tree:
            return existing
        raise SubmitError(f"{branch} already holds different content; use a new request ID", EXIT_REFUSED)
    commit = gh.post(f"/repos/{repository}/git/commits",
                     {"message": f"Approval request {request_id}", "tree": tree, "parents": []})["sha"]
    gh.post(f"/repos/{repository}/git/refs", {"ref": f"refs/heads/{branch}", "sha": commit})
    return commit


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- runs ----------------------------------------------------------------------

def find_runs(gh: GitHubClient, repository: str, workflow: str, request_id: str, *, limit: int = 300) -> "list[dict]":
    """Runs of the request workflow whose title names `request_id`, newest first."""
    runs = gh.paginate(f"/repos/{repository}/actions/workflows/{workflow}/runs", key="workflow_runs",
                       limit=limit, event="workflow_dispatch")
    found = [run for run in runs if (parse_title(run.get("display_title", "")) or {}).get("id") == request_id]
    return sorted(found, key=lambda run: run.get("created_at") or "", reverse=True)


def dispatch(
    gh: GitHubClient,
    repository: str,
    workflow: str,
    ref: str,
    request: Request,
    *,
    sleep: Sleep,
    attempts: int = 12,
    interval: float = 5.0,
) -> int:
    """Dispatch the request run and return its ID."""
    response = gh.request("POST", f"/repos/{repository}/actions/workflows/{workflow}/dispatches",
                          body={"ref": ref, "inputs": dispatch_inputs(request), "return_run_details": True},
                          ok=(200, 204))
    if isinstance(response, dict) and response.get("workflow_run_id"):
        return int(response["workflow_run_id"])
    # Older API behaviour: no run details, so correlate by the run title.
    for _ in range(attempts):
        runs = find_runs(gh, repository, workflow, request.id, limit=100)
        if runs:
            return int(runs[0]["id"])
        sleep(interval)
    raise SubmitError("the request was dispatched, but its run did not appear; check the Actions tab")


def withdraw(gh: GitHubClient, repository: str, run_id: int) -> None:
    gh.request("POST", f"/repos/{repository}/actions/runs/{run_id}/cancel", ok=(202,))


@dataclass
class Status:
    data: dict

    @property
    def exit_code(self) -> int:
        return int(self.data["exit_code"])

    @property
    def pending(self) -> bool:
        return self.data["state"] != "completed"


def status(gh: GitHubClient, repository: str, run_id: int, *, issue: Optional[int] = None) -> Status:
    run = gh.get(f"/repos/{repository}/actions/runs/{run_id}")
    title = parse_title(run.get("display_title", "")) or {}
    issue = issue or title.get("issue")
    data: dict = {
        "schema": RESULT_SCHEMA,
        "tool_version": TOOL_VERSION,
        "repository": repository,
        "request_id": title.get("id"),
        "action_class": title.get("action_class"),
        "expires_at": title.get("expires"),
        "tracking_issue": issue,
        "run": {"id": run_id, "url": run.get("html_url"), "status": run.get("status"),
                "conclusion": run.get("conclusion"), "attempt": run.get("run_attempt")},
        "state": "completed" if run.get("status") == "completed" else
                 "waiting" if run.get("status") == "waiting" else "running",
        "decision": None,
        "outcome": None,
        "detail": None,
        "reviewers": [],
        "record_url": None,
        "pending": [],
        "notes": [],
    }
    if data["state"] == "waiting":
        for deployment in gh.get(f"/repos/{repository}/actions/runs/{run_id}/pending_deployments") or []:
            data["pending"].append({
                "environment": (deployment.get("environment") or {}).get("name"),
                "current_user_can_approve": bool(deployment.get("current_user_can_approve")),
                "reviewers": [(r.get("reviewer") or {}).get("login") or (r.get("reviewer") or {}).get("slug")
                              for r in deployment.get("reviewers") or []],
            })
        if any(p["current_user_can_approve"] for p in data["pending"]):
            data["notes"].append("this identity can approve its own request; withdraw it and submit as a non-reviewer")
    if data["state"] == "completed":
        found = record.find_records(gh, repository, issue, run_id=run_id) if issue else []
        if found:
            comment, rec = found[-1]
            data.update(decision=rec.get("decision"), outcome=rec.get("outcome"), detail=rec.get("detail"),
                        reviewers=rec.get("reviewers") or [], record_url=comment.get("html_url"))
        else:
            data.update(decision="failed", outcome="record-missing",
                        detail="the run completed without a decision record on the tracking issue")
    data["exit_code"] = exit_code_for(data)
    return Status(data)


def exit_code_for(data: dict) -> int:
    if data["state"] != "completed":
        return EXIT_CODES["pending"]
    if data.get("outcome") == "executed":
        return EXIT_CODES["executed"]
    if data.get("decision") in ("rejected", "expired"):
        return EXIT_CODES[data["decision"]]
    return EXIT_CODES["failed"]


def wait(
    gh: GitHubClient,
    repository: str,
    run_id: int,
    *,
    issue: Optional[int],
    sleep: Sleep,
    clock: Clock,
    timeout: float,
    interval: float = 30.0,
    on_change: Optional[Callable[[Status], None]] = None,
) -> Status:
    deadline = clock() + timedelta(seconds=timeout)
    last_state = None
    while True:
        current = status(gh, repository, run_id, issue=issue)
        if on_change and current.data["state"] != last_state:
            on_change(current)
            last_state = current.data["state"]
        if not current.pending or clock() >= deadline:
            return current
        sleep(interval)


# --- reviewer and housekeeping views ----------------------------------------

def waiting_requests(gh: GitHubClient, repository: str, *, limit: int = 300) -> "list[dict]":
    runs = gh.paginate(f"/repos/{repository}/actions/runs", key="workflow_runs", limit=limit, status="waiting")
    found = []
    for run in runs:
        title = parse_title(run.get("display_title", ""))
        if title:
            found.append({"run": run, "title": title})
    return found


def queue(gh: GitHubClient, repositories: "list[str]") -> "list[dict]":
    """Requests waiting for review, with whether this identity may decide each one."""
    items = []
    for repository in repositories:
        for entry in waiting_requests(gh, repository):
            run, title = entry["run"], entry["title"]
            deployments = gh.get(f"/repos/{repository}/actions/runs/{run['id']}/pending_deployments") or []
            items.append({
                "repository": repository,
                "request_id": title["id"],
                "action_class": title["action_class"],
                "tracking_issue": title["issue"],
                "expires_at": title["expires"],
                "requester": (run.get("triggering_actor") or {}).get("login"),
                "run_url": run.get("html_url"),
                "run_id": run["id"],
                "environments": [(d.get("environment") or {}).get("name") for d in deployments],
                "can_review": any(d.get("current_user_can_approve") for d in deployments),
            })
    return sorted(items, key=lambda item: item["expires_at"])


def sweep(gh: GitHubClient, repository: str, *, now: datetime, dry_run: bool) -> "list[dict]":
    """Cancel requests still waiting after their expiry; the record job then records `expired`."""
    swept = []
    for entry in waiting_requests(gh, repository):
        run, title = entry["run"], entry["title"]
        if parse_time(title["expires"]) > now:
            continue
        if not dry_run:
            withdraw(gh, repository, run["id"])
        swept.append({"request_id": title["id"], "run_id": run["id"], "expires_at": title["expires"],
                      "cancelled": not dry_run})
    return swept


def default_expiry(now: datetime, action_class: ActionClass, hours: Optional[int]) -> datetime:
    return now + timedelta(hours=hours if hours is not None else action_class.default_expiry_hours)
