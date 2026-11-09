"""Minimal GitHub REST and GraphQL client over the standard library.

The transport is injectable so every API interaction can be exercised by
offline fixture tests; nothing here caches, retries silently, or logs a
token.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

API_URL = "https://api.github.com"
API_VERSION = "2026-03-10"
USER_AGENT = "projectbuddy-approval-gate"

# (method, url, headers, body) -> (status, body bytes)
Transport = Callable[[str, str, dict, Optional[bytes]], "tuple[int, bytes]"]


class GitHubError(RuntimeError):
    """A GitHub API call returned an unexpected status."""

    def __init__(self, status: int, message: str, path: str = "") -> None:
        super().__init__(f"GitHub API returned {status} for {path}: {message}")
        self.status = status
        self.path = path
        self.api_message = message


def urllib_transport(method: str, url: str, headers: dict, data: Optional[bytes]) -> "tuple[int, bytes]":  # pragma: no cover - network
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except OSError as exc:
        # Network failures become status 0 so every caller takes its fail-closed GitHubError path.
        raise GitHubError(0, f"network error: {getattr(exc, 'reason', exc)}", urllib.parse.urlsplit(url).path) from exc


class GitHubClient:
    """Small authenticated client. `token` may be None for anonymous reads."""

    def __init__(
        self,
        token: Optional[str],
        *,
        transport: Optional[Transport] = None,
        api_url: str = API_URL,
        api_version: str = API_VERSION,
    ) -> None:
        self._token = token
        self._transport = transport or urllib_transport
        self.api_url = api_url.rstrip("/")
        self.api_version = api_version

    def with_token(self, token: Optional[str]) -> "GitHubClient":
        return GitHubClient(token, transport=self._transport, api_url=self.api_url, api_version=self.api_version)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        body: Any = None,
        ok: "tuple[int, ...]" = (200, 201, 202, 204),
    ) -> Any:
        url = path if path.startswith("https://") else self.api_url + path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": self.api_version,
            "User-Agent": USER_AGENT,
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        status, raw = self._transport(method, url, headers, data)
        if status not in ok:
            raise GitHubError(status, _error_message(raw), path)
        if not raw:
            return None
        return json.loads(raw.decode("utf-8"))

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params=params or None)

    def try_get(self, path: str, **params: Any) -> Any:
        """GET that returns None for 404 instead of raising."""
        try:
            return self.get(path, **params)
        except GitHubError as exc:
            if exc.status == 404:
                return None
            raise

    def post(self, path: str, body: Any = None) -> Any:
        return self.request("POST", path, body=body if body is not None else {})

    def patch(self, path: str, body: Any) -> Any:
        return self.request("PATCH", path, body=body)

    def paginate(self, path: str, *, key: Optional[str] = None, limit: int = 1000, **params: Any) -> list:
        """Collect up to `limit` items across pages of 100."""
        items: list = []
        page = 1
        while len(items) < limit:
            data = self.get(path, per_page=100, page=page, **params)
            batch = data.get(key, []) if key else data
            items.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return items[:limit]

    def graphql(self, query: str, variables: dict) -> dict:
        data = self.request("POST", "/graphql", body={"query": query, "variables": variables})
        if data.get("errors"):
            raise GitHubError(200, "; ".join(str(e.get("message")) for e in data["errors"]), "/graphql")
        return data["data"]


def _error_message(raw: bytes) -> str:
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return raw[:200].decode("utf-8", "replace")
    if isinstance(parsed, dict):
        return str(parsed.get("message", parsed))
    return str(parsed)
