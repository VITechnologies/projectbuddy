"""Approval gate: request model, action-class registry, leak scan, markdown, and GitHub client."""
from __future__ import annotations

import json
import sys
from datetime import timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from approval_gate_fakes import DIGEST, NOW, REPO, SHA_A, FakeGitHub, request_dict  # noqa: E402

from approval_gate import leakscan, markdown, model, registry  # noqa: E402
from approval_gate.github import GitHubClient, GitHubError, _error_message  # noqa: E402


# --- model ---------------------------------------------------------------------------

def test_valid_request_round_trips_to_identical_canonical_json_and_digest():
    request = model.parse_request(request_dict())
    again = model.parse_request(request.canonical_json())
    assert again == request
    assert again.digest() == request.digest() and len(request.digest()) == 64
    assert request.free_text() == {"rationale": "Prove the gate end to end.", "expected_effect": "Nothing is written.",
                                   "rollback": "Nothing to roll back."}
    assert request.content.label() == f"sha256 {DIGEST}"
    assert "is at" in request.precondition.label()


def test_digest_changes_when_any_bound_field_changes():
    base = model.parse_request(request_dict()).digest()
    assert model.parse_request(request_dict(rationale="Different.")).digest() != base
    assert model.parse_request(request_dict(content={"kind": "commit", "value": SHA_A})).digest() != base


@pytest.mark.parametrize("raw, expected", [
    ("{not json", "not valid JSON"),
    ("[1, 2]", "must be a JSON object"),
])
def test_unparseable_requests_are_rejected(raw, expected):
    with pytest.raises(model.RequestError) as caught:
        model.parse_request(raw)
    assert expected in str(caught.value)


def test_every_problem_is_listed_at_once():
    bad = {
        "schema": "other", "id": "X", "action_class": "", "target": "nope",
        "content": {"kind": "commit", "value": "abc1234"},
        "precondition": {"kind": "ref-sha", "repository": "bad", "ref": "main", "sha": "short"},
        "rationale": "", "expected_effect": "x" * 4001, "rollback": None,
        "requested_at": "2026-11-10T11:00:00", "expires_at": "yesterday",
        "tracking_issue": True, "requester": "", "extra": 1,
    }
    with pytest.raises(model.RequestError) as caught:
        model.parse_request(bad)
    problems = " | ".join(caught.value.problems)
    for expected in ("schema must be", "id must be", "action_class must be", "target.repository",
                     "target.ref", "full lowercase commit SHA", "precondition.repository", "precondition.ref",
                     "precondition.sha", "rationale is required", "expected_effect is longer", "rollback is required",
                     "requested_at must be", "expires_at must be", "tracking_issue", "requester must be a GitHub login",
                     "unknown fields: extra"):
        assert expected in problems, expected


@pytest.mark.parametrize("field, value, ok", [
    ("requester", "agent-bot", True),
    ("requester", "projectbuddy-requester[bot]", True),
    ("requester", "bot<!-- approval-gate-record-data: e30= -->", False),
    ("requester", "a`b", False),
    ("requester", "-leading", False),
    ("requester", "x" * 40, False),
    ("action_class", "noop", True),
    ("action_class", "wiki-publish", True),
    ("action_class", "noop`<!--", False),
    ("action_class", "Noop", False),
    ("action_class", "n", False),
])
def test_identity_fields_that_reach_markdown_have_a_strict_format(field, value, ok):
    if ok:
        assert getattr(model.parse_request(request_dict(**{field: value})), field) == value
    else:
        with pytest.raises(model.RequestError) as caught:
            model.parse_request(request_dict(**{field: value}))
        assert f"{field} must be" in str(caught.value)


def test_identifying_fields_cover_every_published_content_binding():
    digest_only = model.parse_request(request_dict()).identifying_fields()
    assert set(digest_only) == {"id", "action_class", "target", "content", "precondition", "requester"}
    staged = model.parse_request(request_dict(content={"kind": "sha256", "value": DIGEST, "ref": "requests/req-001",
                                                       "path": "configs/build01.json"})).identifying_fields()
    assert staged["content.ref"] == "requests/req-001" and staged["content.path"] == "configs/build01.json"


@pytest.mark.parametrize("content, expected", [
    (None, "content is required"),
    ({"kind": "tarball", "value": DIGEST}, "content.kind must be one of"),
    ({"kind": "sha256", "value": "ABC"}, "64-character lowercase SHA-256"),
    ({"kind": "sha256", "value": DIGEST, "ref": "feature/x"}, "staging branch"),
    ({"kind": "sha256", "value": DIGEST, "ref": "requests/req-001"}, "content.path must name"),
    ({"kind": "sha256", "value": DIGEST, "ref": "requests/req-001", "path": "../escape"}, "content.path must name"),
    ({"kind": "commit", "value": SHA_A, "path": "file.txt"}, "applies only to sha256"),
])
def test_content_binding_rules(content, expected):
    with pytest.raises(model.RequestError) as caught:
        model.parse_request(request_dict(content=content))
    assert expected in str(caught.value)


def test_staged_content_and_commit_content_parse():
    staged = model.parse_request(request_dict(content={"kind": "sha256", "value": DIGEST, "ref": "requests/req-001",
                                                       "path": "docs/plan.md"}))
    assert staged.content.to_dict()["path"] == "docs/plan.md"
    commit = model.parse_request(request_dict(content={"kind": "commit", "value": SHA_A, "ref": "requests/req-001"}))
    assert commit.content.to_dict() == {"kind": "commit", "value": SHA_A, "ref": "requests/req-001"}


@pytest.mark.parametrize("precondition, expected", [
    (None, "precondition is required"),
    ({"kind": "mood", "repository": REPO}, "precondition.kind must be one of"),
    ({"kind": "issue-edited", "repository": REPO, "issue": 0, "last_edited_at": None}, "precondition.issue"),
    ({"kind": "issue-edited", "repository": REPO, "issue": 3}, "last_edited_at is required"),
    ({"kind": "issue-edited", "repository": REPO, "issue": 3, "last_edited_at": "soon"}, "ISO 8601 timestamp or null"),
])
def test_precondition_rules(precondition, expected):
    with pytest.raises(model.RequestError) as caught:
        model.parse_request(request_dict(precondition=precondition))
    assert expected in str(caught.value)


def test_issue_precondition_normalizes_its_timestamp_and_labels_never_edited():
    edited = model.parse_request(request_dict(precondition={
        "kind": "issue-edited", "repository": REPO, "issue": 3, "last_edited_at": "2026-11-10T12:30:00+02:00"}))
    assert edited.precondition.last_edited_at == "2026-11-10T10:30:00Z"
    assert edited.precondition.to_dict()["issue"] == 3
    never = model.parse_request(request_dict(precondition={
        "kind": "issue-edited", "repository": REPO, "issue": 3, "last_edited_at": None}))
    assert "never edited" in never.precondition.label()


def test_expiry_must_follow_the_request_time():
    with pytest.raises(model.RequestError, match="later than requested_at"):
        model.parse_request(request_dict(expires_at="2026-11-10T10:00:00Z"))


def test_time_helpers_reject_naive_and_non_string_values():
    assert model.format_time(NOW) == "2026-11-10T12:00:00Z"
    assert model.parse_time("2026-11-10T12:00:00Z").tzinfo == timezone.utc
    with pytest.raises(ValueError):
        model.parse_time("2026-11-10T12:00:00")
    with pytest.raises(ValueError):
        model.parse_time(5)


# --- registry ------------------------------------------------------------------------

def registry_doc(**class_fields) -> dict:
    noop = {"executor": "noop", "environment": "approval-noop", "targets": ["@self"], "refs": ["@default"]}
    noop.update(class_fields)
    return {"schema": "approval-gate/classes@1", "classes": {"noop": noop}}


def test_missing_registry_file_means_only_the_builtin_noop_class(tmp_path):
    assert registry.load_registry(None) == {"noop": registry.NOOP_CLASS}
    assert registry.load_registry(tmp_path / "absent.json") == {"noop": registry.NOOP_CLASS}


def test_registry_file_parses_with_defaults(tmp_path):
    path = tmp_path / "classes.json"
    path.write_text(json.dumps(registry_doc(description="demo")), encoding="utf-8")
    parsed = registry.load_registry(path)["noop"]
    assert parsed.default_expiry_hours == registry.DEFAULT_EXPIRY_HOURS
    assert parsed.allow_digest_only is False and "runner" not in parsed.to_dict()
    assert parsed.to_dict()["description"] == "demo"


def test_invalid_registry_json_is_reported(tmp_path):
    path = tmp_path / "classes.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(registry.RegistryError, match="not valid JSON"):
        registry.load_registry(path)
    path.unlink()
    path.mkdir()
    with pytest.raises(registry.RegistryError, match="cannot be read"):
        registry.load_registry(path)


@pytest.mark.parametrize("doc, expected", [
    ({"schema": "nope"}, "schema must be"),
    ({"schema": "approval-gate/classes@1", "classes": {}}, "non-empty object"),
    ({"schema": "approval-gate/classes@1", "classes": {"Bad Name": {}}}, "name must be"),
    ({"schema": "approval-gate/classes@1", "classes": {"noop": []}}, "must be an object"),
    (registry_doc(executor="shell"), "executor must be one of"),
    (registry_doc(environment="has space"), "GitHub environment name"),
    (registry_doc(targets=[]), "targets must be a non-empty list"),
    (registry_doc(refs=["main"]), "refs must be '@default'"),
    (registry_doc(content_kinds=["tarball"]), "content_kinds must be drawn"),
    (registry_doc(precondition_kinds=["mood"]), "precondition_kinds must be drawn"),
    (registry_doc(max_expiry_hours=721), "max_expiry_hours must be an integer"),
    (registry_doc(default_expiry_hours=True), "default_expiry_hours must be an integer"),
    (registry_doc(max_expiry_hours=10, default_expiry_hours=20), "exceeds max_expiry_hours"),
    (registry_doc(runner="self-hosted"), "unknown key.*'runner'"),
    (registry_doc(max_expiry_hour=24), "unknown key.*'max_expiry_hour'"),
    (dict(registry_doc(), owner="x"), "unknown key.*'owner'"),
    (registry_doc(allow_digest_only="yes"), "allow_digest_only must be"),
    (registry_doc(description=5), "description must be a string"),
])
def test_registry_rejects_invalid_classes(doc, expected):
    with pytest.raises(registry.RegistryError, match=expected):
        registry.parse_registry(doc)


def test_two_classes_cannot_share_one_environment():
    doc = registry_doc()
    doc["classes"]["other"] = dict(doc["classes"]["noop"], environment="APPROVAL-NOOP")
    with pytest.raises(registry.RegistryError, match="share environment"):
        registry.parse_registry(doc)


def test_target_patterns_and_tokens():
    noop = registry.NOOP_CLASS
    assert noop.target_allowed(REPO, "refs/heads/main", self_repository=REPO.upper(), default_branch="main")
    assert not noop.target_allowed(REPO, "refs/heads/dev", self_repository=REPO, default_branch="main")
    assert not noop.target_allowed(REPO, "refs/heads/main", self_repository=REPO, default_branch=None)
    assert not noop.target_allowed("octo-org/other", "refs/heads/main", self_repository=REPO, default_branch="main")
    wild = registry.parse_registry(registry_doc(targets=["octo-org/*", "@self"], refs=["refs/heads/release/*", "refs/heads/main"]))["noop"]
    assert wild.target_allowed("Octo-Org/Wiki", "refs/heads/release/1.0", self_repository=REPO, default_branch="main")
    assert wild.has_wildcards() and not noop.has_wildcards()
    assert wild.concrete_targets(self_repository=REPO, default_branch="main") == [(REPO, "refs/heads/main")]
    assert noop.concrete_targets(self_repository=REPO, default_branch=None) == []


# --- leak scan -----------------------------------------------------------------------

@pytest.mark.parametrize("text, pattern", [
    ("token ghp_" + "A" * 36, "github-token"),
    ("github_pat_" + "B" * 30, "github-token"),
    ("glpat-" + "C" * 20, "gitlab-token"),
    ("AKIA" + "D" * 16, "aws-access-key"),
    ("xoxb-" + "1" * 12, "slack-token"),
    ("-----BEGIN RSA PRIVATE KEY-----", "private-key"),
    ("eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 12, "jwt"),
    ("password = hunter2hunter2", "credential-assignment"),
    ("see /home/alice/notes", "local-path"),
    ("see C:\\Users\\alice\\notes", "local-path"),
])
def test_builtin_patterns_flag_secrets_and_local_paths(text, pattern):
    findings = leakscan.scan({"rationale": text})
    assert [f.pattern for f in findings] == [pattern]
    assert findings[0].describe() == f"rationale: matched {pattern}"
    assert text not in findings[0].describe()


def test_ordinary_text_and_empty_fields_pass():
    assert leakscan.scan([("rationale", "Merge the reviewed release notes."), ("empty", "")]) == []
    assert leakscan.scan({"rationale": "password policy requires rotation"}) == []


def test_private_patterns_are_case_insensitive_and_invalid_ones_still_match_literally():
    patterns = "# comment\n\ninternal-host\\d+\n[unclosed\n"
    findings = leakscan.scan({"a": "INTERNAL-HOST42", "b": "see [unclosed bracket"}, patterns)
    assert [(f.field, f.pattern) for f in findings] == [("a", "private-identifier-3"), ("b", "private-identifier-4")]


TOKEN = "ghp_" + "Z" * 36


@pytest.mark.parametrize("data", [
    TOKEN.encode("utf-8"),
    ("\ufeff" + TOKEN).encode("utf-16-le"),
    ("\ufeff" + TOKEN).encode("utf-16-be"),
    TOKEN.encode("utf-16-le"),
    TOKEN.encode("utf-32-le"),
    "café ".encode("cp1252") + TOKEN.encode("ascii"),
    b"\x89PNG\x00\xff" + TOKEN.encode("ascii") + b"\x00\x01",
], ids=["utf-8", "utf-16-le-bom", "utf-16-be-bom", "utf-16-le", "utf-32-le", "cp1252", "binary"])
def test_readable_text_exposes_secrets_in_any_encoding(data):
    assert [f.pattern for f in leakscan.scan({"file": leakscan.readable_text(data)})] == ["github-token"]


def test_readable_text_leaves_clean_files_clean():
    assert leakscan.scan({"file": leakscan.readable_text("Notes für heute".encode("utf-16"))}) == []


# --- markdown ------------------------------------------------------------------------

def test_markdown_helpers_keep_text_verbatim():
    assert markdown.cell("a|b\nc") == "a\\|b c"
    assert markdown.html("<b>&") == "&lt;b&gt;&amp;"
    assert markdown.fenced("plain") == ["```text", "plain", "```"]
    assert markdown.fenced("has ```` fence", indent="  ")[0] == "  `````text"
    assert markdown.longest_backtick_run("a`b``c") == 2


@pytest.mark.parametrize("text, expected", [
    ("notes/plan.md", "`notes/plan.md`"),
    ("a`b", "``a`b``"),
    ("`edge`", "`` `edge` ``"),
    ("x|y\nz", "`x\\|y z`"),
])
def test_code_spans_cannot_be_broken_out_of(text, expected):
    assert markdown.code(text) == expected


# --- GitHub client -------------------------------------------------------------------

def test_client_sends_auth_and_version_headers_and_decodes_json():
    fake = FakeGitHub().add("GET", "/repos/o/r", {"ok": True})
    gh = GitHubClient("tok", transport=fake, api_url="https://api.example/")
    assert gh.get("/repos/o/r", ref="main") == {"ok": True}
    call = fake.calls[0]
    assert call.query == {"ref": "main"}
    assert call.headers["Authorization"].endswith(" tok")
    assert call.headers["X-GitHub-Api-Version"] and "Content-Type" not in call.headers


def test_client_posts_json_and_handles_empty_bodies():
    fake = FakeGitHub().add("POST", "/x", None, status=204).add("PATCH", "/y", {"done": 1})
    gh = GitHubClient(None, transport=fake)
    assert gh.post("/x") is None
    assert fake.calls[0].body == {} and "Authorization" not in fake.calls[0].headers
    assert gh.patch("/y", {"a": 1}) == {"done": 1}
    assert gh.request("GET", "https://api.github.com/x?a=1", params={"b": 2}, ok=(404,)) == {"message": "Not Found"}
    assert fake.calls[-1].query == {"a": "1", "b": "2"}


def test_client_errors_and_try_get():
    fake = FakeGitHub().add("GET", "/boom", {"message": "nope"}, status=500).add("GET", "/raw", b"\xff plain", status=502)
    gh = GitHubClient("t", transport=fake)
    assert gh.try_get("/missing") is None
    with pytest.raises(GitHubError) as caught:
        gh.try_get("/boom")
    assert caught.value.status == 500 and caught.value.api_message == "nope" and caught.value.path == "/boom"
    with pytest.raises(GitHubError, match="plain"):
        gh.get("/raw")
    assert _error_message(b"[1]") == "[1]"
    assert _error_message(b'{"other": 1}') == "{'other': 1}"


def test_paginate_follows_full_pages_and_honours_the_limit():
    pages = {"1": list(range(100)), "2": list(range(100, 150))}
    fake = FakeGitHub().handle("GET", "/items", lambda call: (200, {"items": pages[call.query["page"]]}))
    gh = GitHubClient("t", transport=fake)
    assert len(gh.paginate("/items", key="items")) == 150
    assert len(gh.paginate("/items", key="items", limit=120)) == 120
    plain = FakeGitHub().add("GET", "/list", [1, 2])
    assert GitHubClient("t", transport=plain).paginate("/list") == [1, 2]


def test_graphql_returns_data_or_raises_errors():
    fake = FakeGitHub().seq("POST", "/graphql", [(200, {"data": {"x": 1}}), (200, {"errors": [{"message": "bad"}]})])
    gh = GitHubClient("t", transport=fake)
    assert gh.graphql("query", {}) == {"x": 1}
    with pytest.raises(GitHubError, match="bad"):
        gh.graphql("query", {})
    assert gh.with_token("other").api_url == gh.api_url
