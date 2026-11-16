"""Offline fakes shared by the approval gate tests: a routed GitHub transport and request fixtures."""
from __future__ import annotations

import base64
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

REPO = "octo-org/gate"
SHA_A = "a" * 40
SHA_B = "b" * 40
DIGEST = "c" * 64
NOW = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)
REQUESTER = "pb-agent[bot]"
REVIEWER = "alice"


@dataclass
class Call:
    method: str
    path: str
    query: dict
    headers: dict
    body: Any


class FakeGitHub:
    """Transport that answers from routes keyed by (method, path); unknown routes are 404.

    A route's value is a (status, body) pair, a list of pairs consumed in order
    (the last one repeats), or a callable taking the Call.
    """

    def __init__(self) -> None:
        self.routes: dict = {}
        self.calls: "list[Call]" = []

    def add(self, method: str, path: str, body: Any = None, status: int = 200) -> "FakeGitHub":
        self.routes[(method, path)] = (status, body)
        return self

    def seq(self, method: str, path: str, responses: "list[tuple[int, Any]]") -> "FakeGitHub":
        self.routes[(method, path)] = list(responses)
        return self

    def handle(self, method: str, path: str, handler: Callable[[Call], "tuple[int, Any]"]) -> "FakeGitHub":
        self.routes[(method, path)] = handler
        return self

    def __call__(self, method: str, url: str, headers: dict, data: Optional[bytes]) -> "tuple[int, bytes]":
        parsed = urlparse(url)
        call = Call(method, parsed.path, {k: v[0] for k, v in parse_qs(parsed.query).items()}, headers,
                    json.loads(data) if data else None)
        self.calls.append(call)
        route = self.routes.get((method, parsed.path))
        if route is None:
            status, body = 404, {"message": "Not Found"}
        elif callable(route):
            status, body = route(call)
        elif isinstance(route, list):
            status, body = route.pop(0) if len(route) > 1 else route[0]
        else:
            status, body = route
        if body is None:
            return status, b""
        if isinstance(body, bytes):
            return status, body
        return status, json.dumps(body).encode("utf-8")

    def called(self, method: str, path: str) -> "list[Call]":
        return [call for call in self.calls if call.method == method and call.path == path]


def request_dict(**overrides: Any) -> dict:
    data = {
        "schema": "approval-gate/request@1",
        "id": "req-001",
        "action_class": "noop",
        "target": {"repository": REPO, "ref": "refs/heads/main"},
        "content": {"kind": "sha256", "value": DIGEST},
        "precondition": {"kind": "ref-sha", "repository": REPO, "ref": "refs/heads/main", "sha": SHA_A},
        "rationale": "Prove the gate end to end.",
        "expected_effect": "Nothing is written.",
        "rollback": "Nothing to roll back.",
        "requested_at": "2026-11-10T11:00:00Z",
        "expires_at": "2026-11-11T11:00:00Z",
        "tracking_issue": 7,
        "requester": REQUESTER,
    }
    data.update(overrides)
    return data


def environment(
    *,
    users: "tuple[str, ...]" = (REVIEWER,),
    teams: "tuple[str, ...]" = (),
    prevent_self_review: bool = True,
    can_admins_bypass: Optional[bool] = False,
    branch_policy: bool = True,
    wait_timer: Optional[int] = None,
) -> dict:
    reviewers = [{"type": "User", "reviewer": {"login": login}} for login in users]
    reviewers += [{"type": "Team", "reviewer": {"slug": slug}} for slug in teams]
    rules: list = []
    if reviewers:
        rules.append({"type": "required_reviewers", "prevent_self_review": prevent_self_review, "reviewers": reviewers})
    if wait_timer is not None:
        rules.append({"type": "wait_timer", "wait_timer": wait_timer})
    data: dict = {"name": "approval-noop", "protection_rules": rules,
                  "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True}
                  if branch_policy else None}
    if can_admins_bypass is not None:
        data["can_admins_bypass"] = can_admins_bypass
    return data


def ref_route(fake: FakeGitHub, repository: str, branch: str, sha: str) -> None:
    fake.add("GET", f"/repos/{repository}/git/ref/heads/{branch}", {"object": {"sha": sha}})


def file_content(data: bytes) -> dict:
    return {"type": "file", "encoding": "base64", "content": base64.b64encode(data).decode("ascii")}


def gate_repo(fake: FakeGitHub, *, permissions: Optional[dict] = None, visibility: str = "public",
              owner_type: str = "Organization") -> None:
    data: dict = {"full_name": REPO, "default_branch": "main", "visibility": visibility,
                  "private": visibility != "public", "owner": {"login": "octo-org", "type": owner_type}}
    if permissions is not None:
        data["permissions"] = permissions
    fake.add("GET", f"/repos/{REPO}", data)


def run_payload(*, run_id: int = 99, status: str = "in_progress", conclusion: Optional[str] = None,
                actor: str = REQUESTER, head_branch: str = "main", event: str = "workflow_dispatch",
                attempt: int = 1, title: Optional[str] = None) -> dict:
    return {
        "id": run_id,
        "status": status,
        "conclusion": conclusion,
        "event": event,
        "head_branch": head_branch,
        "run_attempt": attempt,
        "html_url": f"https://github.com/{REPO}/actions/runs/{run_id}",
        "triggering_actor": {"login": actor},
        "display_title": title or "Approval request req-001 · noop · #7 · expires 2026-11-11T11:00:00Z",
        "created_at": "2026-11-10T11:00:05Z",
    }
