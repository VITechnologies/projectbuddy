"""Readiness: is each action class enforcing, advisory, not ready, or unsupported?

`enforcing` means a human decision is the only way the class's effect can
happen: the environment is fully protected, the requester is known and is not
a reviewer, and the requester cannot write the gate or any target directly.
`advisory` means the gate works but the requester could also act without it.
`not-ready` lists each missing setting with its fix. `unsupported` means the
repository's plan cannot require reviewers at all (REQ-096, TEST-098, TEST-102).

Run it with the requester's credentials: write access is evaluated for the
identity the tool is authenticated as.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import READINESS_SCHEMA, TOOL_VERSION
from .auth import Identity
from .checks import is_eligible_reviewer, read_environment
from .github import GitHubClient, GitHubError
from .registry import ActionClass

STATUS_ORDER = ("enforcing", "advisory", "not-ready", "unsupported")

FIXES = {
    "environment-missing": "Create the environment with required reviewers, prevent self-review, no administrator "
                           "bypass, and deployment branches limited to the default branch.",
    "environment-unreadable": "Run readiness with a token that can read this repository's environments.",
    "required-reviewers-missing": "Add at least one required reviewer (user or team) who is not the requester.",
    "self-review-allowed": "Enable 'Prevent self-review' on the environment.",
    "admin-bypass-allowed": "Disable 'Allow administrators to bypass configured protection rules'.",
    "branch-policy-missing": "Set deployment branches to 'Selected branches and tags' with only the default branch.",
    "requester-unknown": "Authenticate as the requester (GitHub App, user token, or set APPROVAL_GATE_REQUESTER).",
    "requester-is-reviewer": "Submit as an identity that is not a required reviewer, such as a GitHub App.",
    "reviewer-membership-unknown": "Grant the requester read access to organization members, or list users instead of teams.",
    "requester-is-admin": "Remove administrator access from the requester; an administrator can change the environment.",
    "gate-writable": "Protect the default branch with a ruleset the requester cannot bypass, so it cannot change the gate.",
    "requester-can-write": "Remove the requester's write access to the target, or protect the target ref with a "
                           "ruleset the requester cannot bypass.",
    "targets-not-enumerable": "List concrete target repositories and refs instead of wildcard patterns.",
    "plan-unsupported": "Make the repository public, or move it to a plan that allows required reviewers in private repositories.",
    "plan-unknown": "Run readiness with a token that can read the organization's plan.",
}


@dataclass
class Finding:
    code: str
    message: str

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "fix": FIXES.get(self.code, "")}


@dataclass
class ClassReadiness:
    name: str
    environment: str
    status: str = "enforcing"
    findings: "list[Finding]" = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"name": self.name, "environment": self.environment, "status": self.status,
                "findings": [finding.to_dict() for finding in self.findings]}


def worst(statuses: "list[str]") -> str:
    return max(statuses, key=STATUS_ORDER.index) if statuses else "not-ready"


def plan_findings(gh: GitHubClient, repo: dict) -> "list[Finding]":
    """Private and internal repositories need GitHub Enterprise for required reviewers."""
    if repo.get("visibility", "private" if repo.get("private") else "public") == "public":
        return []
    owner = repo.get("owner") or {}
    if owner.get("type") != "Organization":
        return [Finding("plan-unsupported", "required reviewers are unavailable in private repositories owned by a user account")]
    try:
        organization = gh.get(f"/orgs/{owner.get('login')}")
    except GitHubError:
        organization = {}
    plan = ((organization or {}).get("plan") or {}).get("name")
    if plan is None:
        return [Finding("plan-unknown", "the organization's plan is not readable, so required-reviewer support is unconfirmed")]
    if plan.lower() != "enterprise":
        return [Finding("plan-unsupported", f"the {plan} plan does not offer required reviewers in private repositories")]
    return []


def can_write(gh: GitHubClient, repository: str, branch: str) -> "tuple[bool, str]":
    """Whether the authenticated identity can update `branch` directly; unknown counts as yes."""
    try:
        repo = gh.get(f"/repos/{repository}")
    except GitHubError as exc:
        return True, f"{repository} unreadable ({exc.status}), so write access is unconfirmed"
    permissions = repo.get("permissions")
    if permissions is None:
        return True, f"permissions on {repository} are not reported for this identity"
    if not (permissions.get("push") or permissions.get("maintain") or permissions.get("admin")):
        return False, f"no write permission on {repository}"
    try:
        rules = gh.get(f"/repos/{repository}/rules/branches/{branch}")
    except GitHubError:
        rules = []
    for rule in rules or []:
        if rule.get("type") not in ("update", "pull_request"):
            continue
        if rule["type"] == "pull_request" and int((rule.get("parameters") or {}).get("required_approving_review_count") or 0) < 1:
            continue
        try:
            ruleset = gh.get(f"/repos/{repository}/rulesets/{rule.get('ruleset_id')}")
        except GitHubError:
            continue
        if ruleset.get("current_user_can_bypass") == "never":
            return False, f"{repository} {branch} is protected by ruleset {ruleset.get('name') or rule.get('ruleset_id')} that this identity cannot bypass"
    return True, f"this identity can push to {repository} {branch}"


def check(
    gh: GitHubClient,
    *,
    repository: str,
    registry: "dict[str, ActionClass]",
    identity: Identity,
    only: Optional[str] = None,
) -> dict:
    repo = gh.get(f"/repos/{repository}")
    default = repo.get("default_branch")
    global_findings = plan_findings(gh, repo)
    unsupported = any(finding.code == "plan-unsupported" for finding in global_findings)
    permissions = repo.get("permissions") or {}
    gate_write: Optional[tuple] = None
    results = []
    for action_class in registry.values():
        if only and action_class.name != only:
            continue
        result = ClassReadiness(action_class.name, action_class.environment)
        result.findings.extend(global_findings)
        protection = read_environment(gh, repository, action_class.environment)
        result.findings.extend(Finding(code, message) for code, message in protection.problems())
        if identity.login is None:
            result.findings.append(Finding("requester-unknown", "the requester identity is unknown"))
        elif protection.exists and protection.readable:
            eligible = is_eligible_reviewer(gh, protection, identity.login)
            if eligible is True:
                result.findings.append(Finding("requester-is-reviewer", f"{identity.login} is an eligible reviewer"))
            elif eligible is None:
                result.findings.append(Finding("reviewer-membership-unknown",
                                               f"team membership of {identity.login} could not be read"))
        if permissions.get("admin"):
            result.findings.append(Finding("requester-is-admin", f"{identity.login or 'the requester'} administers {repository}"))
        blocking = [f for f in result.findings if f.code not in ("gate-writable", "requester-can-write", "targets-not-enumerable")]
        if unsupported:
            result.status = "unsupported"
        elif blocking:
            result.status = "not-ready"
        else:
            if gate_write is None:
                gate_write = can_write(gh, repository, default) if default else (True, "default branch unknown")
            if gate_write[0]:
                result.findings.append(Finding("gate-writable", gate_write[1]))
            if action_class.has_wildcards():
                result.findings.append(Finding("targets-not-enumerable", "targets or refs use wildcard patterns"))
            for target_repository, ref in action_class.concrete_targets(self_repository=repository, default_branch=default):
                branch = ref[len("refs/heads/"):]
                if target_repository.lower() == repository.lower() and branch == default:
                    continue  # already reported as gate-writable
                writable, detail = can_write(gh, target_repository, branch)
                if writable:
                    result.findings.append(Finding("requester-can-write", detail))
            result.status = "advisory" if result.findings else "enforcing"
        results.append(result)
    return {
        "schema": READINESS_SCHEMA,
        "tool_version": TOOL_VERSION,
        "repository": repository,
        "visibility": repo.get("visibility", "private" if repo.get("private") else "public"),
        "requester": identity.to_dict(),
        "status": worst([result.status for result in results]),
        "classes": [result.to_dict() for result in results],
    }
