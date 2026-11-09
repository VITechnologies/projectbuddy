"""Credentials and identity for the submission tool.

Token order: `APPROVAL_GATE_TOKEN`, `GH_TOKEN`, `GITHUB_TOKEN`, then
`gh auth token`. When `APPROVAL_GATE_APP_ID` and
`APPROVAL_GATE_APP_KEY_FILE` are set, the tool instead mints a short-lived
GitHub App installation token per session (ADR-0007 D1), scoped to the
repositories the request touches, so the requester is `<app-slug>[bot]` and
never a human reviewer. Tokens are never printed or logged.
"""
from __future__ import annotations

import base64
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Mapping, Optional, Sequence

from .github import GitHubClient, GitHubError

TOKEN_VARIABLES = ("APPROVAL_GATE_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")
ACTIONS_BOT = "github-actions[bot]"

# (argv, stdin bytes) -> (exit code, stdout bytes)
Runner = Callable[[Sequence[str], Optional[bytes]], "tuple[int, bytes]"]


class AuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class Identity:
    login: Optional[str]
    kind: str  # user, app, actions, or unknown
    source: str

    def to_dict(self) -> dict:
        return {"login": self.login, "kind": self.kind, "source": self.source}


def subprocess_runner(argv: Sequence[str], stdin: Optional[bytes]) -> "tuple[int, bytes]":  # pragma: no cover - process
    try:
        completed = subprocess.run(list(argv), input=stdin, capture_output=True, check=False, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return 127, b""
    return completed.returncode, completed.stdout


def resolve_token(environ: Mapping[str, str], runner: Runner) -> "tuple[Optional[str], str]":
    for name in TOKEN_VARIABLES:
        value = environ.get(name, "").strip()
        if value:
            return value, name
    code, out = runner(["gh", "auth", "token"], None)
    token = out.decode("utf-8", "replace").strip() if code == 0 else ""
    return (token, "gh-cli") if token else (None, "anonymous")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def app_jwt(app_id: str, key_file: str, now: datetime, runner: Runner) -> str:
    """RS256 JWT for the App, signed by openssl so no crypto package is needed."""
    issued = int(now.timestamp()) - 60
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64url(json.dumps({"iat": issued, "exp": issued + 600, "iss": str(app_id)}, separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}".encode("ascii")
    code, signature = runner(["openssl", "dgst", "-sha256", "-sign", key_file], signing_input)
    if code != 0 or not signature:
        raise AuthError("openssl could not sign the GitHub App JWT; check APPROVAL_GATE_APP_KEY_FILE")
    return f"{header}.{payload}.{_b64url(signature)}"


def app_installation_token(
    gh: GitHubClient,
    *,
    app_id: str,
    key_file: str,
    repositories: Sequence[str],
    now: datetime,
    runner: Runner,
) -> "tuple[str, str]":
    """(installation token, '<slug>[bot]') limited to `repositories`, which share one owner."""
    owners = {repository.partition("/")[0].lower() for repository in repositories}
    if len(owners) != 1:
        raise AuthError("a GitHub App token covers one owner; the gate and target repositories have different owners")
    app = gh.with_token(app_jwt(app_id, key_file, now, runner))
    try:
        slug = app.get("/app")["slug"]
        installation = app.get(f"/repos/{repositories[0]}/installation")["id"]
        names = sorted({repository.partition("/")[2] for repository in repositories})
        token = app.request("POST", f"/app/installations/{installation}/access_tokens",
                            body={"repositories": names}, ok=(201,))["token"]
    except GitHubError as exc:
        raise AuthError(f"GitHub App authentication failed: {exc.api_message} ({exc.status})") from None
    return token, f"{slug}[bot]"


def identify(gh: GitHubClient, source: str, environ: Mapping[str, str]) -> Identity:
    if source == "anonymous":
        return Identity(None, "unknown", source)
    if source == "github-app":
        raise AuthError("identify the App from app_installation_token instead")
    try:
        user = gh.get("/user")
    except GitHubError:
        user = None
    if user and user.get("login"):
        return Identity(user["login"], "app" if user.get("type") == "Bot" else "user", source)
    if source == "GITHUB_TOKEN" and environ.get("GITHUB_ACTIONS") == "true":
        return Identity(ACTIONS_BOT, "actions", source)
    # An installation token minted elsewhere cannot name itself; the gate's
    # preflight still checks the declared login against the run's actor.
    declared = environ.get("APPROVAL_GATE_REQUESTER", "").strip()
    if declared:
        return Identity(declared, "declared", source)
    return Identity(None, "unknown", source)


def authenticate(
    gh: GitHubClient,
    *,
    environ: Mapping[str, str],
    repositories: Sequence[str],
    now: datetime,
    runner: Runner,
) -> "tuple[GitHubClient, Identity]":
    app_id = environ.get("APPROVAL_GATE_APP_ID", "").strip()
    key_file = environ.get("APPROVAL_GATE_APP_KEY_FILE", "").strip()
    if app_id or key_file:
        if not (app_id and key_file):
            raise AuthError("set both APPROVAL_GATE_APP_ID and APPROVAL_GATE_APP_KEY_FILE, or neither")
        token, login = app_installation_token(gh, app_id=app_id, key_file=key_file, repositories=repositories,
                                              now=now, runner=runner)
        return gh.with_token(token), Identity(login, "app", "github-app")
    token, source = resolve_token(environ, runner)
    client = gh.with_token(token)
    return client, identify(client, source, environ)
