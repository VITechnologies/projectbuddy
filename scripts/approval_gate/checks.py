"""Live-state checks shared by preflight, verify, readiness, and submit.

Every check fails closed: when GitHub cannot answer, the answer is "no".
"""
from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from typing import Optional

from .github import GitHubClient, GitHubError
from .model import Precondition, Request

ISSUE_EDITED_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) { issue(number: $number) { lastEditedAt } }
}
"""


@dataclass
class CheckResult:
    ok: bool
    detail: str
    # The staged file's bytes once they match the bound digest, for the leak scan.
    data: Optional[bytes] = field(default=None, repr=False)


def resolve_ref(gh: GitHubClient, repository: str, ref: str) -> Optional[str]:
    """Commit SHA a full ref (or a bare branch name) points at, or None if absent."""
    name = ref[len("refs/"):] if ref.startswith("refs/") else f"heads/{ref}"
    data = gh.try_get(f"/repos/{repository}/git/ref/{name}")
    if not data:
        return None
    return data.get("object", {}).get("sha")


def issue_last_edited(gh: GitHubClient, repository: str, number: int) -> Optional[str]:
    owner, _, name = repository.partition("/")
    data = gh.graphql(ISSUE_EDITED_QUERY, {"owner": owner, "name": name, "number": number})
    issue = (data.get("repository") or {}).get("issue")
    if issue is None:
        raise GitHubError(404, f"issue #{number} not found", repository)
    return issue.get("lastEditedAt")


def check_precondition(gh: GitHubClient, precondition: Precondition) -> CheckResult:
    try:
        if precondition.kind == "ref-sha":
            current = resolve_ref(gh, precondition.repository, precondition.ref)
            if current == precondition.sha:
                return CheckResult(True, f"{precondition.ref} is still at {current}")
            return CheckResult(False, f"{precondition.ref} is at {current or 'nothing (missing)'}, expected {precondition.sha}")
        current = issue_last_edited(gh, precondition.repository, precondition.issue)
        expected = precondition.last_edited_at
        if _same_instant(current, expected):
            return CheckResult(True, f"issue #{precondition.issue} body unchanged since {expected or 'creation'}")
        return CheckResult(False, f"issue #{precondition.issue} body edited at {current}, expected {expected or 'never edited'}")
    except GitHubError as exc:
        return CheckResult(False, f"precondition unreadable: {exc.api_message} ({exc.status})")


def _same_instant(current: Optional[str], expected: Optional[str]) -> bool:
    if current is None or expected is None:
        return current is None and expected is None
    from .model import parse_time

    try:
        return parse_time(current) == parse_time(expected)
    except ValueError:
        return False


def check_content(gh: GitHubClient, request: Request, *, gate_repository: str, allow_digest_only: bool) -> CheckResult:
    """Recompute the content binding (REQ-094): the executor stops on any mismatch."""
    content = request.content
    try:
        if content.kind == "commit":
            if content.ref:
                current = resolve_ref(gh, gate_repository, f"refs/heads/{content.ref}")
                if current != content.value:
                    return CheckResult(False, f"staged ref {content.ref} is at {current or 'nothing (missing)'}, expected {content.value}")
                return CheckResult(True, f"staged ref {content.ref} is at the approved commit")
            commit = gh.try_get(f"/repos/{request.target_repository}/commits/{content.value}")
            if not commit or commit.get("sha") != content.value:
                return CheckResult(False, f"commit {content.value} not found in {request.target_repository}")
            return CheckResult(True, f"commit {content.value} exists in {request.target_repository}")
        if not content.ref:
            if allow_digest_only:
                return CheckResult(True, "digest-only content; the action class verifies the payload")
            return CheckResult(False, "digest-only content is not allowed for this action class")
        data = gh.try_get(f"/repos/{gate_repository}/contents/{content.path}", ref=content.ref)
        if not data or data.get("type") != "file" or data.get("encoding") != "base64":
            return CheckResult(False, f"staged file {content.path} not found on {content.ref}")
        payload = base64.b64decode(data.get("content", ""))
        digest = hashlib.sha256(payload).hexdigest()
        if digest != content.value:
            return CheckResult(False, f"staged file {content.path} has SHA-256 {digest}, expected {content.value}")
        return CheckResult(True, f"staged file {content.path} matches the approved SHA-256", data=payload)
    except GitHubError as exc:
        return CheckResult(False, f"content unreadable: {exc.api_message} ({exc.status})")


@dataclass
class EnvironmentProtection:
    name: str
    exists: bool
    reviewer_users: "list[str]" = field(default_factory=list)
    reviewer_teams: "list[str]" = field(default_factory=list)
    has_required_reviewers: bool = False
    prevent_self_review: bool = False
    admin_bypass_disabled: bool = False
    branch_policy_set: bool = False
    wait_timer: int = 0
    readable: bool = True

    def problems(self) -> "list[tuple[str, str]]":
        """(code, message) for every missing protection; empty means protected."""
        if not self.readable:
            return [("environment-unreadable", f"environment {self.name!r} could not be read")]
        if not self.exists:
            return [("environment-missing",
                     f"environment {self.name!r} does not exist; a run would create it with no protection")]
        found = []
        if not self.has_required_reviewers:
            found.append(("required-reviewers-missing", f"environment {self.name!r} has no required reviewers"))
        if not self.prevent_self_review:
            found.append(("self-review-allowed", f"environment {self.name!r} does not prevent self-review"))
        if not self.admin_bypass_disabled:
            found.append(("admin-bypass-allowed", f"environment {self.name!r} lets administrators bypass review"))
        if not self.branch_policy_set:
            found.append(("branch-policy-missing",
                          f"environment {self.name!r} accepts runs from any branch; restrict it to the default branch"))
        return found


def read_environment(gh: GitHubClient, repository: str, name: str) -> EnvironmentProtection:
    try:
        data = gh.try_get(f"/repos/{repository}/environments/{name}")
    except GitHubError:
        return EnvironmentProtection(name=name, exists=False, readable=False)
    if data is None:
        return EnvironmentProtection(name=name, exists=False)
    protection = EnvironmentProtection(name=name, exists=True)
    # GitHub's default allows administrator bypass; only an explicit false disables it.
    protection.admin_bypass_disabled = data.get("can_admins_bypass") is False
    protection.branch_policy_set = data.get("deployment_branch_policy") is not None
    for rule in data.get("protection_rules") or []:
        if rule.get("type") == "required_reviewers":
            protection.prevent_self_review = rule.get("prevent_self_review") is True
            for entry in rule.get("reviewers") or []:
                reviewer = entry.get("reviewer") or {}
                if entry.get("type") == "User" and reviewer.get("login"):
                    protection.reviewer_users.append(reviewer["login"])
                elif entry.get("type") == "Team" and reviewer.get("slug"):
                    organization = (reviewer.get("organization") or {}).get("login") or repository.partition("/")[0]
                    protection.reviewer_teams.append(f"{organization}/{reviewer['slug']}")
            protection.has_required_reviewers = bool(protection.reviewer_users or protection.reviewer_teams)
        elif rule.get("type") == "wait_timer":
            protection.wait_timer = int(rule.get("wait_timer") or 0)
    return protection


def is_eligible_reviewer(gh: GitHubClient, protection: EnvironmentProtection, login: str) -> Optional[bool]:
    """True or False when known; None when a team's membership cannot be read."""
    if login.lower() in (user.lower() for user in protection.reviewer_users):
        return True
    unknown = False
    for team in protection.reviewer_teams:
        organization, _, slug = team.partition("/")
        try:
            # A token that cannot see the team also gets 404 for the membership,
            # so a missing membership only counts once the team itself is visible.
            if gh.try_get(f"/orgs/{organization}/teams/{slug}") is None:
                unknown = True
                continue
            membership = gh.try_get(f"/orgs/{organization}/teams/{slug}/memberships/{login}")
        except GitHubError:
            unknown = True
            continue
        if membership and membership.get("state") in ("active", "pending"):
            return True
    return None if unknown else False


def default_branch(gh: GitHubClient, repository: str) -> Optional[str]:
    data = gh.try_get(f"/repos/{repository}")
    return data.get("default_branch") if data else None
