"""The request document (`approval-gate/request@1`) and its validation.

A request binds everything a reviewer approves (REQ-094): its ID, action
class, target repository and ref, content by full commit SHA or SHA-256
digest, the expected target precondition, and the rationale. It is
canonicalized to JSON so the same bytes can be digested by the requester,
shown to the reviewer, and re-checked by the executor.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from . import REQUEST_SCHEMA

REQUEST_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,62}$")
FULL_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
BRANCH_REF_RE = re.compile(r"^refs/heads/(?!.*\.\.)(?!.*//)[A-Za-z0-9._/-]+(?<![./])$")
STAGING_REF_RE = re.compile(r"^requests/[a-z0-9][a-z0-9._-]{2,62}$")
CONTENT_PATH_RE = re.compile(r"^(?!/)(?!.*\.\.)[A-Za-z0-9._/-]{1,200}$")
ACTION_CLASS_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
# A GitHub user login, or `<app-slug>[bot]` for a GitHub App.
LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?:\[bot\])?$")

FREE_TEXT_FIELDS = ("rationale", "expected_effect", "rollback")
FREE_TEXT_MAX = 4000
CONTENT_KINDS = ("commit", "sha256")
PRECONDITION_KINDS = ("ref-sha", "issue-edited")


class RequestError(ValueError):
    """The request document is invalid. `problems` lists every reason."""

    def __init__(self, problems: "list[str]") -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Content:
    kind: str
    value: str
    ref: Optional[str] = None
    path: Optional[str] = None

    def to_dict(self) -> dict:
        data: dict = {"kind": self.kind, "value": self.value}
        if self.ref is not None:
            data["ref"] = self.ref
        if self.path is not None:
            data["path"] = self.path
        return data

    def label(self) -> str:
        return f"{self.kind} {self.value}"


@dataclass(frozen=True)
class Precondition:
    kind: str
    repository: str
    ref: Optional[str] = None
    sha: Optional[str] = None
    issue: Optional[int] = None
    last_edited_at: Optional[str] = None

    def to_dict(self) -> dict:
        data: dict = {"kind": self.kind, "repository": self.repository}
        if self.kind == "ref-sha":
            data.update(ref=self.ref, sha=self.sha)
        else:
            data.update(issue=self.issue, last_edited_at=self.last_edited_at)
        return data

    def label(self) -> str:
        if self.kind == "ref-sha":
            return f"`{self.repository}` `{self.ref}` is at `{self.sha}`"
        edited = self.last_edited_at or "never edited"
        return f"`{self.repository}#{self.issue}` body last edited: {edited}"


@dataclass(frozen=True)
class Request:
    id: str
    action_class: str
    target_repository: str
    target_ref: str
    content: Content
    precondition: Precondition
    rationale: str
    expected_effect: str
    rollback: str
    requested_at: datetime
    expires_at: datetime
    tracking_issue: int
    requester: str

    def to_dict(self) -> dict:
        return {
            "schema": REQUEST_SCHEMA,
            "id": self.id,
            "action_class": self.action_class,
            "target": {"repository": self.target_repository, "ref": self.target_ref},
            "content": self.content.to_dict(),
            "precondition": self.precondition.to_dict(),
            "rationale": self.rationale,
            "expected_effect": self.expected_effect,
            "rollback": self.rollback,
            "requested_at": format_time(self.requested_at),
            "expires_at": format_time(self.expires_at),
            "tracking_issue": self.tracking_issue,
            "requester": self.requester,
        }

    def canonical_json(self) -> str:
        return canonical_json(self.to_dict())

    def digest(self) -> str:
        """SHA-256 of the canonical request; shown to the reviewer and recorded."""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def free_text(self) -> "dict[str, str]":
        return {name: getattr(self, name) for name in FREE_TEXT_FIELDS}

    def identifying_fields(self) -> "dict[str, str]":
        """Every value other than free text that a summary or decision record publishes, for the leak scan."""
        fields = {"id": self.id, "action_class": self.action_class,
                  "target": f"{self.target_repository} {self.target_ref}", "content": self.content.label(),
                  "precondition": self.precondition.label(), "requester": self.requester}
        if self.content.ref:
            fields["content.ref"] = self.content.ref
        if self.content.path:
            fields["content.path"] = self.content.path
        return fields


def canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("not a timestamp string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp has no time zone")
    return parsed.astimezone(timezone.utc)


def parse_request(raw: "str | dict") -> Request:
    """Parse and validate a request document; raise RequestError listing every problem."""
    problems: list = []
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise RequestError([f"request is not valid JSON: {exc.msg}"]) from None
    else:
        data = raw
    if not isinstance(data, dict):
        raise RequestError(["request must be a JSON object"])

    if data.get("schema") != REQUEST_SCHEMA:
        problems.append(f"schema must be {REQUEST_SCHEMA!r}")

    request_id = data.get("id")
    if not isinstance(request_id, str) or not REQUEST_ID_RE.match(request_id):
        problems.append("id must be 3-63 lowercase letters, digits, '.', '_' or '-', starting with a letter or digit")

    action_class = data.get("action_class")
    if not isinstance(action_class, str) or not ACTION_CLASS_RE.match(action_class):
        problems.append("action_class must be 2-41 lowercase letters, digits, or '-', starting with a letter")

    target = data.get("target") if isinstance(data.get("target"), dict) else {}
    target_repository = target.get("repository")
    target_ref = target.get("ref")
    if not isinstance(target_repository, str) or not REPOSITORY_RE.match(target_repository):
        problems.append("target.repository must be 'owner/name'")
    if not isinstance(target_ref, str) or not BRANCH_REF_RE.match(target_ref):
        problems.append("target.ref must be a full branch ref such as 'refs/heads/main'")

    content = _parse_content(data.get("content"), problems)
    precondition = _parse_precondition(data.get("precondition"), problems)

    for name in FREE_TEXT_FIELDS:
        value = data.get(name)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"{name} is required")
        elif len(value) > FREE_TEXT_MAX:
            problems.append(f"{name} is longer than {FREE_TEXT_MAX} characters")

    requested_at = _parse_time_field(data, "requested_at", problems)
    expires_at = _parse_time_field(data, "expires_at", problems)
    if requested_at and expires_at and expires_at <= requested_at:
        problems.append("expires_at must be later than requested_at")

    tracking_issue = data.get("tracking_issue")
    if isinstance(tracking_issue, bool) or not isinstance(tracking_issue, int) or tracking_issue < 1:
        problems.append("tracking_issue must be a positive issue number")

    requester = data.get("requester")
    if not isinstance(requester, str) or not LOGIN_RE.match(requester):
        problems.append("requester must be a GitHub login, or '<app-slug>[bot]' for a GitHub App")

    unknown = sorted(set(data) - {
        "schema", "id", "action_class", "target", "content", "precondition", "requested_at",
        "expires_at", "tracking_issue", "requester", *FREE_TEXT_FIELDS,
    })
    if unknown:
        problems.append(f"unknown fields: {', '.join(unknown)}")

    if problems:
        raise RequestError(problems)
    return Request(
        id=request_id,
        action_class=action_class,
        target_repository=target_repository,
        target_ref=target_ref,
        content=content,
        precondition=precondition,
        rationale=data["rationale"],
        expected_effect=data["expected_effect"],
        rollback=data["rollback"],
        requested_at=requested_at,
        expires_at=expires_at,
        tracking_issue=tracking_issue,
        requester=requester,
    )


def _parse_time_field(data: dict, name: str, problems: list) -> Optional[datetime]:
    try:
        return parse_time(data.get(name))
    except ValueError:
        problems.append(f"{name} must be an ISO 8601 UTC timestamp")
        return None


def _parse_content(raw: Any, problems: list) -> Optional[Content]:
    if not isinstance(raw, dict):
        problems.append("content is required: a full commit SHA or a SHA-256 digest")
        return None
    kind = raw.get("kind")
    value = raw.get("value")
    ref = raw.get("ref")
    path = raw.get("path")
    before = len(problems)
    if kind not in CONTENT_KINDS:
        problems.append(f"content.kind must be one of {', '.join(CONTENT_KINDS)}")
    elif kind == "commit" and not (isinstance(value, str) and FULL_SHA_RE.match(value)):
        problems.append("content.value must be a full lowercase commit SHA, not a branch, tag, or abbreviated SHA")
    elif kind == "sha256" and not (isinstance(value, str) and SHA256_RE.match(value)):
        problems.append("content.value must be a 64-character lowercase SHA-256 digest")
    if ref is not None and not (isinstance(ref, str) and STAGING_REF_RE.match(ref)):
        problems.append("content.ref must be a staging branch named 'requests/<request-id>'")
    if kind == "sha256" and ref is not None and not (isinstance(path, str) and CONTENT_PATH_RE.match(path)):
        problems.append("content.path must name the staged file whose SHA-256 is content.value")
    if kind == "commit" and path is not None:
        problems.append("content.path applies only to sha256 content")
    if len(problems) != before:
        return None
    return Content(kind=kind, value=value, ref=ref, path=path)


def _parse_precondition(raw: Any, problems: list) -> Optional[Precondition]:
    if not isinstance(raw, dict):
        problems.append("precondition is required")
        return None
    kind = raw.get("kind")
    repository = raw.get("repository")
    before = len(problems)
    if kind not in PRECONDITION_KINDS:
        problems.append(f"precondition.kind must be one of {', '.join(PRECONDITION_KINDS)}")
    if not isinstance(repository, str) or not REPOSITORY_RE.match(repository):
        problems.append("precondition.repository must be 'owner/name'")
    if kind == "ref-sha":
        ref = raw.get("ref")
        sha = raw.get("sha")
        if not isinstance(ref, str) or not BRANCH_REF_RE.match(ref):
            problems.append("precondition.ref must be a full branch ref")
        if not isinstance(sha, str) or not FULL_SHA_RE.match(sha):
            problems.append("precondition.sha must be a full commit SHA")
        if len(problems) == before:
            return Precondition(kind=kind, repository=repository, ref=ref, sha=sha)
    elif kind == "issue-edited":
        issue = raw.get("issue")
        edited = raw.get("last_edited_at", "missing")
        if isinstance(issue, bool) or not isinstance(issue, int) or issue < 1:
            problems.append("precondition.issue must be a positive issue number")
        if edited == "missing":
            problems.append("precondition.last_edited_at is required (null when never edited)")
        elif edited is not None:
            try:
                edited = parse_time(edited)
            except ValueError:
                problems.append("precondition.last_edited_at must be an ISO 8601 timestamp or null")
        if len(problems) == before:
            return Precondition(
                kind=kind,
                repository=repository,
                issue=issue,
                last_edited_at=format_time(edited) if edited else None,
            )
    return None
