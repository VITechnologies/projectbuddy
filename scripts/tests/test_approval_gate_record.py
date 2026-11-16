"""Approval gate: decision records, readiness levels, and authentication."""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from approval_gate_fakes import (  # noqa: E402
    NOW, REPO, REQUESTER, REVIEWER, FakeGitHub, environment, gate_repo, request_dict,
)

from approval_gate import auth, readiness, record  # noqa: E402
from approval_gate.github import GitHubClient  # noqa: E402
from approval_gate.model import parse_request  # noqa: E402
from approval_gate.registry import NOOP_CLASS, parse_registry  # noqa: E402
from approval_gate.verify import Review  # noqa: E402

ENV_PATH = f"/repos/{REPO}/environments/approval-noop"
COMMENTS = f"/repos/{REPO}/issues/7/comments"
REQUEST = parse_request(request_dict())


def client(fake: FakeGitHub) -> GitHubClient:
    return GitHubClient("token", transport=fake)


# --- decide ---------------------------------------------------------------------------

def decide(verdict=None, *, request=REQUEST, preflight_ok=True, execute_result="success", reviews=(), now=NOW):
    return record.decide(request=request, request_id="req-001", preflight_ok=preflight_ok,
                         execute_result=execute_result, verdict=verdict, reviews=list(reviews), now=now)


@pytest.mark.parametrize("verdict, expected", [
    ({"ok": True, "detail": "approved by alice", "execution": {"writes": 0}}, ("approved", "executed")),
    ({"ok": True, "execution": {"error": "KeyError: boom"}}, ("failed", "failed:executor-error")),
    ({"ok": False, "reason": "rejected"}, ("rejected", "refused:rejected")),
    ({"ok": False, "reason": "expired"}, ("expired", "refused:expired")),
    ({"ok": False, "reason": "drift"}, ("approved", "refused:drift")),
    ({"ok": False, "reason": "digest-mismatch"}, ("approved", "refused:digest-mismatch")),
    ({"ok": False, "reason": "self-approved"}, ("failed", "refused:self-approved")),
])
def test_verdicts_map_to_the_four_decisions(verdict, expected):
    decision, outcome, _ = decide(verdict)
    assert (decision, outcome) == expected and decision in record.DECISIONS


def test_runs_without_a_verdict_are_still_decided():
    assert decide(preflight_ok=False)[:2] == ("failed", "not-run:preflight")
    assert decide(request=None)[:2] == ("failed", "not-run:preflight")
    rejection = [Review(REVIEWER, "rejected", "")]
    assert decide(execute_result="failure", reviews=rejection)[:2] == ("rejected", "not-run")
    assert decide(execute_result="cancelled", now=NOW + timedelta(days=2))[:2] == ("expired", "not-run:expired")
    assert decide(execute_result="cancelled")[:2] == ("failed", "not-run:withdrawn")
    assert decide(execute_result="failure")[:2] == ("failed", "not-run:failure")
    assert decide(execute_result="")[:2] == ("failed", "not-run:unknown")


# --- records (TEST-101) ---------------------------------------------------------------

def build(**overrides):
    options = dict(request=REQUEST, request_id="req-001", action_class=NOOP_CLASS, decision="rejected",
                   outcome="refused:rejected", detail="rejected by alice", reviews=[], run=record.RunContext(REPO, 99, 1),
                   now=NOW, decided_at="2026-11-10T12:30:00Z", execution=None)
    options.update(overrides)
    return record.build_record(**options)


def test_rejection_with_reason_round_trips_through_the_comment():
    rec = build(reviews=[Review(REVIEWER, "rejected", "Rollback plan is missing ``` here.")])
    body = record.render_comment(rec)
    assert body.startswith(record.marker("req-001", 99, 1))
    assert "**rejected**" in body and "  ````text" in body and "Rollback plan is missing" in body
    assert f"| Request digest | `{REQUEST.digest()}` |" in body and "environment `approval-noop`" in body
    parsed = record.parse_comment(body)
    assert parsed == json.loads(json.dumps(rec))
    assert parsed["reviewers"][0]["comment_missing"] is False and parsed["request_withheld"] is False


def test_rejection_without_reason_and_leaking_comments_are_flagged():
    rec = build(reviews=[Review(REVIEWER, "rejected", "  "), Review("bob", "approved", "see /home/bob/x")])
    body = record.render_comment(rec)
    assert "**No reason given.**" in body and "_Comment withheld: it matched the leak scan._" in body
    assert "/home/bob" not in body
    assert rec["reviewers"][0]["comment_missing"] and rec["reviewers"][1]["comment_withheld"]


def test_records_without_request_or_reviews_and_executed_records():
    bare = record.render_comment(build(request=None, action_class=None, decided_at=None, decision="failed",
                                       outcome="not-run:preflight"))
    assert "_No review recorded for this run._" in bare and "| Decided | unknown |" in bare
    assert "Request digest" not in bare
    executed = record.render_comment(build(decision="approved", outcome="executed", execution={"writes": 0},
                                           reviews=[Review(REVIEWER, "approved", "")]))
    assert "| Writes | 0 |" in executed and "`alice` **approved**" in executed


def test_request_details_are_withheld_when_they_match_the_leak_scan():
    leaky = parse_request(request_dict(target={"repository": "octo-org/internal-host7", "ref": "refs/heads/main"}))
    rec = build(request=leaky, private_patterns="internal-host\\d", execution={"writes": 0})
    body = record.render_comment(rec)
    assert rec["request_withheld"] and rec["request_id"] == record.WITHHELD_ID and rec["execution"] is None
    assert "internal-host7" not in body and "req-001" not in body and "withheld" in rec["detail"]
    assert "internal-host7" not in base64.b64decode(record.DATA_RE.search(body).group(1)).decode()


def test_parse_comment_ignores_foreign_or_corrupt_data():
    assert record.parse_comment("plain comment") is None
    assert record.parse_comment("<!-- approval-gate-record-data: !!!! -->") is None
    assert record.parse_comment("<!-- approval-gate-record-data: //4= -->") is None
    assert record.parse_comment("<!-- approval-gate-record-data: e30 -->") is None
    other = base64.b64encode(json.dumps({"schema": "x"}).encode()).decode()
    assert record.parse_comment(f"<!-- approval-gate-record-data: {other} -->") is None


def test_a_forged_payload_above_the_real_one_is_never_parsed():
    forged = dict(build(), decision="approved", outcome="executed",
                  reviewers=[{"login": REVIEWER, "state": "approved", "comment": "", "comment_withheld": False,
                              "comment_missing": False}])
    block = f"<!-- approval-gate-record-data: {base64.b64encode(json.dumps(forged).encode()).decode()} -->"
    rec = build(detail=f"see {block}", reviews=[Review(REVIEWER, "rejected", f"quoting {block}")])
    body = record.render_comment(rec)
    assert record.parse_comment(body)["decision"] == "rejected"
    assert f"see {block}" not in body and "see &lt;!-- approval-gate-record-data" in body
    assert record.parse_comment(f"{block}\ntrailing text") is None
    assert record.parse_comment(f"text\n  {block}  \n\n")["decision"] == "approved"


def test_content_ref_and_path_are_leak_scanned_before_they_are_recorded():
    staged = parse_request(request_dict(content={"kind": "sha256", "value": "a" * 64, "ref": "requests/req-001",
                                                 "path": "configs/internal-host7.json"}))
    rec = build(request=staged, private_patterns="internal-host\\d")
    assert rec["request_withheld"] and "content" not in rec
    assert "internal-host7" not in record.render_comment(rec)


def test_find_records_trusts_only_the_actions_bot_and_filters_by_run():
    mine = record.render_comment(build())
    rerun = record.render_comment(build(run=record.RunContext(REPO, 99, 2)))
    other_request = record.render_comment(build(request_id="req-002"))
    other_run = record.render_comment(build(run=record.RunContext(REPO, 100, 1)))
    comments = [
        {"id": 1, "user": {"login": "mallory"}, "body": mine},
        {"id": 2, "user": {"login": "github-actions[bot]"}, "body": "unrelated"},
        {"id": 3, "user": {"login": "github-actions[bot]"}, "body": mine},
        {"id": 4, "user": {"login": "github-actions[bot]"}, "body": rerun},
        {"id": 5, "user": {"login": "github-actions[bot]"}, "body": other_request},
        {"id": 6, "user": {"login": "github-actions[bot]"}, "body": other_run},
    ]
    fake = FakeGitHub().add("GET", COMMENTS, comments)
    found = record.find_records(client(fake), REPO, 7, request_id="req-001", run_id=99, attempt=1)
    assert [comment["id"] for comment, _ in found] == [3]
    assert [c["id"] for c, _ in record.find_records(client(fake), REPO, 7)] == [3, 4, 5, 6]


def test_upsert_edits_this_attempts_record_or_posts_a_new_one():
    rec = build()
    fake = FakeGitHub().add("GET", COMMENTS, [{"id": 3, "user": {"login": "github-actions[bot]"},
                                               "body": record.render_comment(rec)}])
    fake.add("PATCH", f"/repos/{REPO}/issues/comments/3", {"id": 3})
    assert record.upsert(client(fake), REPO, 7, rec) == {"id": 3}
    fresh = FakeGitHub().add("GET", COMMENTS, []).add("POST", COMMENTS, {"id": 8}, status=201)
    assert record.upsert(client(fresh), REPO, 7, rec) == {"id": 8}
    assert fresh.called("POST", COMMENTS)[0].body["body"].startswith("<!-- approval-gate-record:")


def test_decided_at_uses_the_gated_job_times():
    jobs = [{"name": "Gate / Preflight", "started_at": "x"},
            {"name": "Gate / Execute approved request", "started_at": "2026-11-10T12:30:00Z",
             "completed_at": "2026-11-10T12:31:00Z"}]
    assert record.decided_at(jobs, "approved") == "2026-11-10T12:30:00Z"
    assert record.decided_at(jobs, "rejected") == "2026-11-10T12:31:00Z"
    assert record.decided_at([], "approved") is None
    assert record.RunContext(REPO, 5, 1).url.endswith(f"/{REPO}/actions/runs/5")


# --- readiness (TEST-098, TEST-102) ------------------------------------------------------

AGENT = auth.Identity(REQUESTER, "app", "github-app")


def readiness_fake(*, permissions=None, env=None, **repo) -> FakeGitHub:
    fake = FakeGitHub()
    gate_repo(fake, permissions=permissions if permissions is not None else {"push": False, "admin": False}, **repo)
    fake.add("GET", ENV_PATH, env or environment())
    return fake


def classes(**fields):
    raw = {"executor": "noop", "environment": "approval-noop", "targets": ["@self"], "refs": ["@default"]}
    raw.update(fields)
    return parse_registry({"schema": "approval-gate/classes@1", "classes": {"noop": raw}})


def check(fake, *, registry=None, identity=AGENT, only=None):
    return readiness.check(client(fake), repository=REPO, registry=registry or {"noop": NOOP_CLASS},
                           identity=identity, only=only)


def codes(result):
    return [finding["code"] for finding in result["classes"][0]["findings"]]


def test_a_fully_protected_class_for_a_read_only_requester_is_enforcing():
    result = check(readiness_fake())
    assert result["status"] == "enforcing" and codes(result) == []
    assert result["schema"] == "approval-gate/readiness@1" and result["requester"]["login"] == REQUESTER


def test_a_requester_that_can_push_makes_the_class_advisory_unless_a_ruleset_blocks_it():
    fake = readiness_fake(permissions={"push": True})
    result = check(fake)
    assert result["status"] == "advisory" and codes(result) == ["gate-writable"]
    assert result["classes"][0]["findings"][0]["fix"].startswith("Protect the default branch")
    fake.add("GET", f"/repos/{REPO}/rules/branches/main", [
        {"type": "pull_request", "ruleset_id": 1, "parameters": {"required_approving_review_count": 0}},
        {"type": "required_signatures", "ruleset_id": 2},
        {"type": "update", "ruleset_id": 3},
        {"type": "pull_request", "ruleset_id": 4, "parameters": {"required_approving_review_count": 1}}])
    fake.add("GET", f"/repos/{REPO}/rulesets/3", {"message": "x"}, status=500)
    fake.add("GET", f"/repos/{REPO}/rulesets/4", {"name": "main", "current_user_can_bypass": "never"})
    assert check(fake)["status"] == "enforcing"


def test_wildcard_and_writable_targets_are_advisory_findings():
    fake = readiness_fake()
    fake.add("GET", "/repos/octo-org/site", {"permissions": {"maintain": True}})
    registry = classes(targets=["@self", "octo-org/site", "octo-org/*"], refs=["@default", "refs/heads/gh-pages"])
    result = check(fake, registry=registry)
    assert result["status"] == "advisory"
    assert codes(result).count("requester-can-write") == 2 and "targets-not-enumerable" in codes(result)


@pytest.mark.parametrize("setup, code", [
    (dict(env=environment(can_admins_bypass=True)), "admin-bypass-allowed"),
    (dict(env=environment(users=(REQUESTER,))), "requester-is-reviewer"),
    (dict(env=environment(users=(), teams=("maintainers",))), "reviewer-membership-unknown"),
    (dict(permissions={"push": True, "admin": True}), "requester-is-admin"),
])
def test_missing_settings_make_the_class_not_ready(setup, code):
    result = check(readiness_fake(**setup))
    assert result["status"] == "not-ready" and code in codes(result)


def test_missing_environment_and_unknown_requester_are_not_ready():
    fake = FakeGitHub()
    gate_repo(fake, permissions={"push": False})
    result = check(fake, identity=auth.Identity(None, "unknown", "anonymous"))
    assert result["status"] == "not-ready"
    assert codes(result) == ["environment-missing", "requester-unknown"]


@pytest.mark.parametrize("repo, org, code", [
    (dict(visibility="private", owner_type="User"), None, "plan-unsupported"),
    (dict(visibility="private"), {"plan": {"name": "team"}}, "plan-unsupported"),
    (dict(visibility="internal"), None, "plan-unknown"),
    (dict(visibility="private"), {"plan": {"name": "enterprise"}}, None),
])
def test_private_repositories_need_an_enterprise_plan(repo, org, code):
    fake = readiness_fake(**repo)
    if org is not None:
        fake.add("GET", "/orgs/octo-org", org)
    result = check(fake)
    if code is None:
        assert result["status"] == "enforcing"
    else:
        assert code in codes(result)
        assert result["status"] == ("unsupported" if code == "plan-unsupported" else "not-ready")


def test_can_write_treats_unknown_access_as_writable():
    assert readiness.can_write(client(FakeGitHub()), "octo-org/x", "main")[0] is True
    fake = FakeGitHub().add("GET", "/repos/octo-org/x", {})
    assert "not reported" in readiness.can_write(client(fake), "octo-org/x", "main")[1]
    assert readiness.worst([]) == "not-ready"


def test_only_checks_the_named_class():
    noop = {"executor": "noop", "environment": "approval-noop", "targets": ["@self"], "refs": ["@default"]}
    registry = parse_registry({"schema": "approval-gate/classes@1",
                               "classes": {"noop": noop, "other": dict(noop, environment="approval-other")}})
    assert [c["name"] for c in check(readiness_fake(), registry=registry, only="noop")["classes"]] == ["noop"]


# --- auth ------------------------------------------------------------------------------

def runner_for(responses):
    calls = []

    def run(argv, stdin):
        calls.append((list(argv), stdin))
        return responses.get(argv[0], (1, b""))

    run.calls = calls
    return run


def test_token_order_and_gh_cli_fallback():
    run = runner_for({"gh": (0, b"cli-token\n")})
    assert auth.resolve_token({"GH_TOKEN": "b", "APPROVAL_GATE_TOKEN": " a "}, run) == ("a", "APPROVAL_GATE_TOKEN")
    assert auth.resolve_token({"GITHUB_TOKEN": "c"}, run) == ("c", "GITHUB_TOKEN")
    assert auth.resolve_token({}, run) == ("cli-token", "gh-cli")
    assert auth.resolve_token({}, runner_for({})) == (None, "anonymous")


def test_identify_names_users_bots_actions_and_declared_requesters():
    gh = client(FakeGitHub().add("GET", "/user", {"login": "Agent", "type": "Bot"}))
    assert auth.identify(gh, "GH_TOKEN", {}) == auth.Identity("Agent", "app", "GH_TOKEN")
    gh = client(FakeGitHub().add("GET", "/user", {"login": "sam", "type": "User"}))
    assert auth.identify(gh, "gh-cli", {}).kind == "user"
    forbidden = client(FakeGitHub().add("GET", "/user", {"message": "x"}, status=403))
    assert auth.identify(forbidden, "GITHUB_TOKEN", {"GITHUB_ACTIONS": "true"}).login == auth.ACTIONS_BOT
    assert auth.identify(forbidden, "GH_TOKEN", {"APPROVAL_GATE_REQUESTER": "bot[bot]"}).kind == "declared"
    assert auth.identify(forbidden, "GH_TOKEN", {}).login is None
    assert auth.identify(forbidden, "anonymous", {}).kind == "unknown"
    with pytest.raises(auth.AuthError):
        auth.identify(forbidden, "github-app", {})


def test_app_jwt_is_signed_by_openssl_and_fails_closed():
    run = runner_for({"openssl": (0, b"\x01\x02")})
    token = auth.app_jwt("123", "key.pem", NOW, run)
    header, payload, signature = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    assert claims == {"iat": int(NOW.timestamp()) - 60, "exp": int(NOW.timestamp()) + 540, "iss": "123"}
    assert run.calls[0][0] == ["openssl", "dgst", "-sha256", "-sign", "key.pem"]
    assert run.calls[0][1] == f"{header}.{payload}".encode()
    with pytest.raises(auth.AuthError, match="openssl"):
        auth.app_jwt("123", "key.pem", NOW, runner_for({}))


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not installed")
def test_app_jwt_signature_verifies_with_a_real_key(tmp_path):
    key = tmp_path / "app.pem"
    public = tmp_path / "app.pub"
    subprocess.run(["openssl", "genrsa", "-out", str(key), "2048"], check=True, capture_output=True)
    subprocess.run(["openssl", "rsa", "-in", str(key), "-pubout", "-out", str(public)], check=True, capture_output=True)
    token = auth.app_jwt("123", str(key), NOW, auth.subprocess_runner)
    header, payload, signature = token.split(".")
    (tmp_path / "sig").write_bytes(base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)))
    verified = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(public), "-signature", str(tmp_path / "sig")],
                              input=f"{header}.{payload}".encode(), capture_output=True)
    assert verified.returncode == 0, verified.stderr


def app_fake(status=201) -> FakeGitHub:
    fake = FakeGitHub().add("GET", "/app", {"slug": "pb-agent"})
    fake.add("GET", f"/repos/{REPO}/installation", {"id": 42})
    fake.add("POST", "/app/installations/42/access_tokens", {"token": "inst-token"}, status=status)
    return fake


def test_app_installation_token_is_scoped_to_the_request_repositories():
    fake = app_fake()
    token, login = auth.app_installation_token(client(fake), app_id="1", key_file="k", repositories=[REPO, "octo-org/site"],
                                               now=NOW, runner=runner_for({"openssl": (0, b"sig")}))
    assert (token, login) == ("inst-token", "pb-agent[bot]")
    assert fake.called("POST", "/app/installations/42/access_tokens")[0].body == {"repositories": ["gate", "site"]}
    with pytest.raises(auth.AuthError, match="one owner"):
        auth.app_installation_token(client(fake), app_id="1", key_file="k", repositories=[REPO, "other/x"],
                                    now=NOW, runner=runner_for({"openssl": (0, b"sig")}))
    with pytest.raises(auth.AuthError, match="App authentication failed"):
        auth.app_installation_token(client(app_fake(status=403)), app_id="1", key_file="k", repositories=[REPO],
                                    now=NOW, runner=runner_for({"openssl": (0, b"sig")}))


def test_authenticate_prefers_the_app_and_requires_both_settings():
    run = runner_for({"openssl": (0, b"sig")})
    gh, identity = auth.authenticate(client(app_fake()), environ={"APPROVAL_GATE_APP_ID": "1",
                                                                 "APPROVAL_GATE_APP_KEY_FILE": "k"},
                                     repositories=[REPO], now=NOW, runner=run)
    assert identity == auth.Identity("pb-agent[bot]", "app", "github-app")
    with pytest.raises(auth.AuthError, match="both"):
        auth.authenticate(client(app_fake()), environ={"APPROVAL_GATE_APP_ID": "1"}, repositories=[REPO], now=NOW, runner=run)
    fake = FakeGitHub().add("GET", "/user", {"login": "sam", "type": "User"})
    gh, identity = auth.authenticate(client(fake), environ={"GH_TOKEN": "t"}, repositories=[REPO], now=NOW, runner=run)
    assert identity.login == "sam" and fake.calls[-1].headers["Authorization"].endswith(" t")
    assert identity.to_dict() == {"login": "sam", "kind": "user", "source": "GH_TOKEN"}
