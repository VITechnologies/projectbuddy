"""Approval gate: live checks, preflight and reviewer summary, approval verification, and executors."""
from __future__ import annotations

import base64
import hashlib
import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from approval_gate_fakes import (  # noqa: E402
    DIGEST, NOW, REPO, REQUESTER, REVIEWER, SHA_A, SHA_B, FakeGitHub, environment, file_content, ref_route,
    request_dict, run_payload,
)

from approval_gate import checks, executors, preflight, verify  # noqa: E402
from approval_gate.github import GitHubClient  # noqa: E402
from approval_gate.model import parse_request  # noqa: E402
from approval_gate.registry import NOOP_CLASS, parse_registry  # noqa: E402

ENV_PATH = f"/repos/{REPO}/environments/approval-noop"
STAGED = {"kind": "commit", "value": SHA_B, "ref": "requests/req-001"}


def client(fake: FakeGitHub) -> GitHubClient:
    return GitHubClient("token", transport=fake)


def strict_class(**fields):
    raw = {"executor": "noop", "environment": "approval-noop", "targets": ["@self"], "refs": ["@default"]}
    raw.update(fields)
    return parse_registry({"schema": "approval-gate/classes@1", "classes": {"noop": raw}})


# --- checks --------------------------------------------------------------------------

def test_resolve_ref_accepts_full_refs_and_branch_names():
    fake = FakeGitHub()
    ref_route(fake, REPO, "main", SHA_A)
    gh = client(fake)
    assert checks.resolve_ref(gh, REPO, "refs/heads/main") == SHA_A
    assert checks.resolve_ref(gh, REPO, "main") == SHA_A
    assert checks.resolve_ref(gh, REPO, "gone") is None
    assert checks.default_branch(gh, REPO) is None


def test_ref_precondition_matches_drifts_and_fails_closed():
    request = parse_request(request_dict())
    fake = FakeGitHub()
    ref_route(fake, REPO, "main", SHA_A)
    assert checks.check_precondition(client(fake), request.precondition).ok
    ref_route(fake, REPO, "main", SHA_B)
    drift = checks.check_precondition(client(fake), request.precondition)
    assert not drift.ok and SHA_B in drift.detail
    missing = checks.check_precondition(client(FakeGitHub()), request.precondition)
    assert not missing.ok and "missing" in missing.detail
    broken = FakeGitHub().add("GET", f"/repos/{REPO}/git/ref/heads/main", {"message": "boom"}, status=500)
    unreadable = checks.check_precondition(client(broken), request.precondition)
    assert not unreadable.ok and "unreadable: boom (500)" in unreadable.detail


@pytest.mark.parametrize("expected, current, ok", [
    ("2026-11-10T10:00:00Z", "2026-11-10T12:00:00+02:00", True),
    (None, None, True),
    ("2026-11-10T10:00:00Z", "2026-11-10T10:05:00Z", False),
    (None, "2026-11-10T10:05:00Z", False),
    ("2026-11-10T10:00:00Z", "garbage", False),
])
def test_issue_precondition_compares_instants(expected, current, ok):
    request = parse_request(request_dict(precondition={"kind": "issue-edited", "repository": REPO, "issue": 3,
                                                       "last_edited_at": expected}))
    fake = FakeGitHub().add("POST", "/graphql", {"data": {"repository": {"issue": {"lastEditedAt": current}}}})
    assert checks.check_precondition(client(fake), request.precondition).ok is ok
    assert fake.calls[0].body["variables"] == {"owner": "octo-org", "name": "gate", "number": 3}


def test_missing_issue_fails_the_precondition():
    request = parse_request(request_dict(precondition={"kind": "issue-edited", "repository": REPO, "issue": 3,
                                                       "last_edited_at": None}))
    fake = FakeGitHub().add("POST", "/graphql", {"data": {"repository": {"issue": None}}})
    result = checks.check_precondition(client(fake), request.precondition)
    assert not result.ok and "not found" in result.detail


def test_content_binding_for_every_content_shape():
    gh_fake = FakeGitHub()
    ref_route(gh_fake, REPO, "requests/req-001", SHA_B)
    gh_fake.add("GET", f"/repos/{REPO}/commits/{SHA_A}", {"sha": SHA_A})
    gh_fake.add("GET", f"/repos/{REPO}/contents/docs/plan.md", file_content(b"plan"))
    gh = client(gh_fake)
    staged_commit = parse_request(request_dict(content=STAGED))
    assert checks.check_content(gh, staged_commit, gate_repository=REPO, allow_digest_only=False).ok
    moved = parse_request(request_dict(content=dict(STAGED, value=SHA_A)))
    assert "expected" in checks.check_content(gh, moved, gate_repository=REPO, allow_digest_only=False).detail
    commit = parse_request(request_dict(content={"kind": "commit", "value": SHA_A}))
    assert checks.check_content(gh, commit, gate_repository=REPO, allow_digest_only=False).ok
    absent = parse_request(request_dict(content={"kind": "commit", "value": SHA_B}))
    assert not checks.check_content(gh, absent, gate_repository=REPO, allow_digest_only=False).ok
    digest_only = parse_request(request_dict())
    assert checks.check_content(gh, digest_only, gate_repository=REPO, allow_digest_only=True).ok
    assert not checks.check_content(gh, digest_only, gate_repository=REPO, allow_digest_only=False).ok
    good = parse_request(request_dict(content={"kind": "sha256", "value": hashlib.sha256(b"plan").hexdigest(),
                                               "ref": "requests/req-001", "path": "docs/plan.md"}))
    assert checks.check_content(gh, good, gate_repository=REPO, allow_digest_only=False).ok
    wrong = parse_request(request_dict(content={"kind": "sha256", "value": DIGEST, "ref": "requests/req-001",
                                                "path": "docs/plan.md"}))
    assert "has SHA-256" in checks.check_content(gh, wrong, gate_repository=REPO, allow_digest_only=False).detail
    missing = parse_request(request_dict(content={"kind": "sha256", "value": DIGEST, "ref": "requests/req-001",
                                                  "path": "docs/none.md"}))
    assert "not found" in checks.check_content(gh, missing, gate_repository=REPO, allow_digest_only=False).detail
    gh_fake.add("GET", f"/repos/{REPO}/commits/{SHA_A}", {"message": "x"}, status=500)
    assert "unreadable" in checks.check_content(gh, commit, gate_repository=REPO, allow_digest_only=False).detail


def test_read_environment_reports_each_missing_protection():
    assert checks.read_environment(client(FakeGitHub()), REPO, "approval-noop").problems()[0][0] == "environment-missing"
    broken = FakeGitHub().add("GET", ENV_PATH, {"message": "x"}, status=403)
    assert checks.read_environment(client(broken), REPO, "approval-noop").problems()[0][0] == "environment-unreadable"
    weak = FakeGitHub().add("GET", ENV_PATH, environment(users=(), prevent_self_review=False, can_admins_bypass=None,
                                                         branch_policy=False, wait_timer=5))
    protection = checks.read_environment(client(weak), REPO, "approval-noop")
    assert [code for code, _ in protection.problems()] == [
        "required-reviewers-missing", "self-review-allowed", "admin-bypass-allowed", "branch-policy-missing"]
    assert protection.wait_timer == 5
    strong = FakeGitHub().add("GET", ENV_PATH, environment(teams=("maintainers",)))
    protection = checks.read_environment(client(strong), REPO, "approval-noop")
    assert protection.problems() == []
    assert protection.reviewer_teams == ["octo-org/maintainers"] and protection.reviewer_users == [REVIEWER]


def test_reviewer_eligibility_is_unknown_unless_team_membership_is_readable():
    protection = checks.EnvironmentProtection("approval-noop", True, reviewer_users=["Alice"],
                                              reviewer_teams=["octo-org/maintainers"])
    teams = "/orgs/octo-org/teams/maintainers"
    assert checks.is_eligible_reviewer(client(FakeGitHub()), protection, "alice") is True
    assert checks.is_eligible_reviewer(client(FakeGitHub()), protection, REQUESTER) is None
    visible = FakeGitHub().add("GET", teams, {"slug": "maintainers"})
    assert checks.is_eligible_reviewer(client(visible), protection, REQUESTER) is False
    visible.add("GET", f"{teams}/memberships/{REQUESTER}", {"state": "pending"})
    assert checks.is_eligible_reviewer(client(visible), protection, REQUESTER) is True
    failing = FakeGitHub().add("GET", teams, {"message": "x"}, status=500)
    assert checks.is_eligible_reviewer(client(failing), protection, REQUESTER) is None


# --- preflight: offline ----------------------------------------------------------------

def offline(raw=None, **kwargs):
    options = dict(registry={"noop": NOOP_CLASS}, self_repository=REPO, default_branch="main", now=NOW)
    options.update(kwargs)
    return preflight.offline_checks(request_dict() if raw is None else raw, **options)


def test_offline_checks_pass_for_a_valid_request():
    report = offline(inputs={"request-id": "req-001", "action-class": "noop", "tracking-issue": "7",
                             "expires-at": "2026-11-11T11:00:00Z"})
    assert report.ok and not report.leak_blocked
    assert [c.id for c in report.checks] == ["request-schema", "inputs-match", "action-class", "target",
                                             "content-kind", "precondition-kind", "expiry", "leak-scan"]


def test_offline_checks_report_each_policy_failure():
    report = offline(request_dict(target={"repository": "octo-org/other", "ref": "refs/heads/main"},
                                  requested_at="2026-11-10T13:00:00Z", expires_at="2026-11-30T13:00:00Z"),
                     registry=strict_class(content_kinds=["commit"], precondition_kinds=["issue-edited"]),
                     inputs={"request-id": "req-002"})
    failed = {c.id: c.detail for c in report.failures()}
    assert set(failed) == {"inputs-match", "target", "content-kind", "precondition-kind", "expiry"}
    assert "request-id, action-class, tracking-issue, expires-at" in failed["inputs-match"]
    assert "requested_at is in the future" in failed["expiry"] and "class maximum" in failed["expiry"]


def test_offline_checks_refuse_expired_unknown_class_and_digest_only():
    expired = offline(now=NOW + timedelta(days=2))
    assert "already expired" in expired.failures()[0].detail
    unknown = offline(request_dict(action_class="deploy"))
    assert [c.id for c in unknown.failures()] == ["action-class"]
    digest_only = offline(registry=strict_class())
    assert [c.id for c in digest_only.failures()] == ["content-kind"]


def test_invalid_request_is_scanned_before_its_problems_are_shown():
    report = offline("{bad json")
    assert [c.id for c in report.checks] == ["request-schema", "leak-scan"] and not report.leak_blocked
    leaking = offline(request_dict(id="BAD", rationale="token ghp_" + "A" * 36))
    assert leaking.leak_blocked and leaking.checks[-1].detail == "blocked: request: matched github-token"


@pytest.mark.parametrize("field, value", [
    ("rationale", "see /home/alice/secret"),
    ("requester", "internal-host7"),
    ("target", {"repository": "octo-org/internal-host7", "ref": "refs/heads/main"}),
])
def test_leaks_in_any_published_field_block_the_request(field, value):
    report = offline(request_dict(**{field: value}), private_patterns="internal-host\\d")
    assert report.leak_blocked and not report.ok


# --- preflight: online and summary ------------------------------------------------------

def online_fake(*, env=None, staged_files=None, tree_error=False) -> FakeGitHub:
    fake = FakeGitHub().add("GET", ENV_PATH, env or environment())
    ref_route(fake, REPO, "main", SHA_A)
    ref_route(fake, REPO, "requests/req-001", SHA_B)
    files = staged_files if staged_files is not None else {"notes/plan.md": b"Plan <b>one</b>"}
    tree = [{"path": path, "type": "blob", "sha": f"blob{i}", "size": len(data)} for i, (path, data) in enumerate(files.items())]
    tree.append({"path": "notes", "type": "tree", "sha": "t"})
    if tree_error:
        fake.add("GET", f"/repos/{REPO}/git/trees/{SHA_B}", {"message": "gone"}, status=404)
    else:
        fake.add("GET", f"/repos/{REPO}/git/trees/{SHA_B}", {"tree": tree, "truncated": False})
    for i, data in enumerate(files.values()):
        fake.add("GET", f"/repos/{REPO}/git/blobs/blob{i}", {"content": base64.b64encode(data).decode()})
    return fake


def run_online(report, fake, **kwargs):
    options = dict(gate_repository=REPO, run_actor=REQUESTER, run_head_branch="main", default_branch="main",
                   probe_secret_visible=False)
    options.update(kwargs)
    return preflight.online_checks(report, client(fake), **options)


def test_online_checks_pass_and_list_staged_content():
    report = run_online(offline(request_dict(content=STAGED)), online_fake())
    assert report.ok, report.failures()
    ids = [c.id for c in report.checks]
    for expected in ("secret-isolation", "requester", "workflow-ref", "environment", "requester-not-reviewer",
                     "precondition", "content", "leak-scan-content"):
        assert expected in ids
    assert report.staged[0].path == "notes/plan.md" and report.staged[0].text == "Plan <b>one</b>"
    summary = preflight.render_summary(report, gate_repository=REPO)
    assert "[!IMPORTANT]" in summary and "Review deployments" in summary
    assert f"| Request digest | `{report.request.digest()}` |" in summary
    assert "<code>notes/plan.md</code>" in summary and "Plan <b>one</b>" in summary
    assert f"/tree/{SHA_B})" in summary and "### Staged content (1 file)" in summary


def test_online_checks_are_skipped_when_offline_checks_failed():
    report = offline(request_dict(action_class="deploy"))
    before = len(report.checks)
    assert len(run_online(report, FakeGitHub()).checks) == before


def test_online_failures_name_each_problem():
    fake = online_fake(env=environment(users=(REQUESTER,)))
    report = run_online(offline(), fake, run_actor="mallory", run_head_branch="requests/req-001",
                        probe_secret_visible=True)
    failed = {c.id for c in report.failures()}
    assert failed == {"secret-isolation", "requester", "workflow-ref", "requester-not-reviewer"}
    missing_env = run_online(offline(), FakeGitHub())
    assert "does not exist" in {c.id: c.detail for c in missing_env.checks}["environment"]
    assert "requester-not-reviewer" not in {c.id for c in missing_env.checks}


def test_unreadable_team_membership_is_noted_but_not_fatal_in_preflight():
    report = run_online(offline(), online_fake(env=environment(users=(), teams=("maintainers",))))
    check = {c.id: c for c in report.checks}["requester-not-reviewer"]
    assert check.ok and "not readable" in check.detail


def test_staged_listing_inlines_small_utf8_files_and_reports_errors():
    files = {"big.bin": b"\xff" * 10}
    files.update({f"f{i:02}.txt": b"x" for i in range(6)})
    files["huge.txt"] = b"y" * (preflight.INLINE_BYTES + 1)
    report = run_online(offline(request_dict(content=STAGED)), online_fake(staged_files=files))
    assert report.ok, report.failures()
    assert report.staged[0].path == "big.bin" and report.staged[0].text is None
    assert sum(1 for f in report.staged if f.text is not None) == preflight.INLINE_FILES
    assert len(report.staged) == 8 and not report.staged_truncated
    errored = run_online(offline(request_dict(content=STAGED)), online_fake(tree_error=True))
    assert "staged content unreadable" in {c.id: c.detail for c in errored.checks}["staged-listing"]


def test_staged_content_the_gate_cannot_scan_whole_is_refused_without_fetching_it():
    many = {f"g{i:02}.txt": b"y" for i in range(preflight.LISTING_LIMIT + 1)}
    large = {"a.bin": b"z" * 10, "b.bin": b"z" * 10}
    cases = [
        (many, None, f"more than {preflight.LISTING_LIMIT} files"),
        (large, {"a.bin": preflight.SCAN_BYTES, "b.bin": 1}, f"over the {preflight.SCAN_BYTES}-byte limit"),
        ({"ok.txt": b"fine"}, "submodule", "1 submodule or other non-file entry"),
    ]
    for files, twist, expected in cases:
        fake = online_fake(staged_files=files)
        tree = fake.routes[("GET", f"/repos/{REPO}/git/trees/{SHA_B}")][1]["tree"]
        if twist == "submodule":
            tree.append({"path": "vendor/lib", "type": "commit", "sha": SHA_A})
        elif twist:
            for entry in tree:
                entry["size"] = twist.get(entry["path"], entry.get("size"))
        report = run_online(offline(request_dict(content=STAGED)), fake)
        check = {c.id: c for c in report.checks}["staged-scannable"]
        assert not check.ok and expected in check.detail and not report.leak_blocked
        assert not any("/git/blobs/" in call.path for call in fake.calls)
        assert all(f.text is None for f in report.staged)
        summary = preflight.render_summary(report, gate_repository=REPO)
        assert "[!CAUTION]" in summary and "<details>" not in summary
    assert run_online(offline(request_dict(content=STAGED)), online_fake(staged_files=many)).staged_truncated


@pytest.mark.parametrize("data", [
    ("\ufeffpassword = 'Sup3rSecretValue1'").encode("utf-16-le"),
    b"caf\xe9 ghp_" + b"Z" * 36,
    b"x" * (preflight.INLINE_BYTES + 1) + b" ghp_" + b"Z" * 36,
], ids=["utf-16", "cp1252", "too-large-to-inline"])
def test_every_staged_file_is_leak_scanned_whatever_its_encoding_or_size(data):
    fake = online_fake(staged_files={"clean.txt": b"ok", "notes.txt": data})
    report = run_online(offline(request_dict(content=STAGED)), fake)
    assert report.leak_blocked and "staged file 2: matched" in {c.id: c.detail for c in report.checks}["leak-scan-content"]


def test_a_staged_digest_bound_file_is_leak_scanned():
    payload = b"token: ghp_" + b"Z" * 36
    content = {"kind": "sha256", "value": hashlib.sha256(payload).hexdigest(), "ref": "requests/req-001",
               "path": "notes/plan.md"}
    fake = online_fake().add("GET", f"/repos/{REPO}/contents/notes/plan.md", file_content(payload))
    report = run_online(offline(request_dict(content=content)), fake)
    assert report.leak_blocked and "staged file 1: matched github-token" in report.failures()[0].detail
    clean = b"plain notes"
    fake = online_fake().add("GET", f"/repos/{REPO}/contents/notes/plan.md", file_content(clean))
    content["value"] = hashlib.sha256(clean).hexdigest()
    assert run_online(offline(request_dict(content=content)), fake).ok


def test_a_failed_blob_fetch_publishes_no_staged_path_or_preview():
    fake = online_fake(staged_files={"a.txt": b"token ghp_" + b"Z" * 36, "b.txt": b"fine"})
    fake.add("GET", f"/repos/{REPO}/git/blobs/blob1", {"message": "Bad Gateway"}, status=502)
    report = run_online(offline(request_dict(content=STAGED)), fake)
    assert not report.ok and not report.leak_blocked and report.staged == []
    assert "staged content unreadable" in {c.id: c.detail for c in report.checks}["staged-listing"]
    summary = preflight.render_summary(report, gate_repository=REPO)
    assert "ghp_" not in summary and "a.txt" not in summary and "### Staged content" not in summary


def test_the_summary_shows_staged_content_only_after_a_passing_content_scan():
    report = run_online(offline(request_dict(content=STAGED)), online_fake())
    assert report.staged_scanned and "### Staged content" in preflight.render_summary(report, gate_repository=REPO)
    report.checks = [c for c in report.checks if c.id != "leak-scan-content"]
    assert not report.staged_scanned and "### Staged content" not in preflight.render_summary(report, gate_repository=REPO)


def test_staged_paths_render_as_unbreakable_code_spans():
    report = run_online(offline(request_dict(content=STAGED)), online_fake(staged_files={"a`b.txt": b"ok"}))
    assert "- ``a`b.txt`` (2 bytes)" in preflight.render_summary(report, gate_repository=REPO)


def test_leaking_staged_content_withholds_the_whole_summary():
    fake = online_fake(staged_files={"internal-host9.txt": b"ok", "b.txt": b"ghp_" + b"Z" * 36})
    report = run_online(offline(request_dict(content=STAGED), private_patterns="internal-host\\d"), fake,
                        private_patterns="internal-host\\d")
    assert report.leak_blocked
    summary = preflight.render_summary(report, gate_repository=REPO, request_id_input="req-001")
    assert "## Approval request `withheld`" in summary and "[!CAUTION]" in summary
    assert "internal-host9" not in summary and "req-001" not in summary and "| Request digest" not in summary
    assert "staged path 1: matched private-identifier-1" in summary and "staged file 2: matched github-token" in summary
    assert "| `content` | pass | withheld |" in summary


def test_summary_for_an_invalid_request_uses_a_valid_input_id_only():
    report = offline("{bad")
    assert "## Approval request `req-009`" in preflight.render_summary(report, gate_repository=REPO, request_id_input="req-009")
    assert "`invalid request`" in preflight.render_summary(report, gate_repository=REPO, request_id_input="Bad Id")


def test_summary_rows_cover_every_inspection_link():
    described = strict_class(allow_digest_only=True, description="Demo | class")
    cases = {
        "digest only": request_dict(),
        f"compare/{SHA_A}...{SHA_B}": request_dict(content={"kind": "commit", "value": SHA_B}),
        f"/commit/{SHA_B}": request_dict(content={"kind": "commit", "value": SHA_B}, precondition={
            "kind": "issue-edited", "repository": REPO, "issue": 3, "last_edited_at": None}),
        "/blob/requests/req-001/docs/a.md": request_dict(content={"kind": "sha256", "value": DIGEST,
                                                                    "ref": "requests/req-001", "path": "docs/a.md"}),
    }
    for expected, raw in cases.items():
        summary = preflight.render_summary(offline(raw, registry=described), gate_repository=REPO)
        assert expected in summary
        assert "(Demo \\| class)" in summary


# --- verify (TEST-099) ----------------------------------------------------------------

def verify_fake(*, run=None, approvals=None, env=None, head=SHA_A) -> FakeGitHub:
    fake = FakeGitHub().add("GET", ENV_PATH, env or environment())
    fake.add("GET", f"/repos/{REPO}/actions/runs/99", run or run_payload())
    fake.add("GET", f"/repos/{REPO}/actions/runs/99/approvals", approvals if approvals is not None else [
        {"state": "approved", "comment": "Looks right.", "user": {"login": REVIEWER},
         "environments": [{"name": "approval-noop"}]}])
    ref_route(fake, REPO, "main", head)
    return fake


def run_verify(fake, *, request=None, job_environment="approval-noop", attempt=1, now=NOW):
    return verify.verify(client(fake), request or parse_request(request_dict()), NOOP_CLASS, gate_repository=REPO,
                         run_id=99, run_attempt=attempt, job_environment=job_environment, default_branch="main", now=now)


def test_a_valid_approval_verifies_and_the_noop_executor_runs_exactly_once():
    verdict = run_verify(verify_fake())
    assert verdict.ok and verdict.reason is None and "approved by alice" in verdict.detail
    assert verdict.to_dict()["reviews"] == [{"login": REVIEWER, "state": "approved", "comment": "Looks right."}]
    calls = []

    def counting(request, action_class, environ):
        calls.append(request.id)
        return executors.noop(request, action_class, environ)

    result = executors.run(parse_request(request_dict()), NOOP_CLASS, {executors.PROBE_SECRET: "set"},
                           executors={"noop": counting})
    assert calls == ["req-001"]
    assert result["writes"] == 0 and result["probe_secret_present"] is True


@pytest.mark.parametrize("setup, reason", [
    (dict(job_environment="production"), "environment-mismatch"),
    (dict(attempt=2), "re-run"),
    (dict(fake=dict(run=run_payload(attempt=2))), "re-run"),
    (dict(fake=dict(run=run_payload(event="push"))), "untrusted-ref"),
    (dict(fake=dict(run=run_payload(head_branch="requests/req-001"))), "untrusted-ref"),
    (dict(fake=dict(run=run_payload(actor="mallory"))), "requester-mismatch"),
    (dict(fake=dict(env=environment(prevent_self_review=False))), "environment-unprotected"),
    (dict(fake=dict(approvals=[{"state": "rejected", "user": {"login": REVIEWER}, "comment": "No.",
                                "environments": [{"name": "approval-noop"}]}])), "rejected"),
    (dict(fake=dict(approvals=[{"state": "approved", "user": {"login": REQUESTER},
                                "environments": [{"name": "approval-noop"}]}])), "self-approved"),
    (dict(fake=dict(approvals=[{"state": "approved", "user": {"login": REVIEWER},
                                "environments": [{"name": "other-env"}]}])), "no-approval"),
    (dict(now=NOW + timedelta(days=2)), "expired"),
    (dict(fake=dict(head=SHA_B)), "drift"),
    (dict(request=parse_request(request_dict(content={"kind": "commit", "value": SHA_B}))), "digest-mismatch"),
])
def test_every_refusal_stops_before_execution(setup, reason):
    fake = verify_fake(**setup.pop("fake", {}))
    verdict = run_verify(fake, **setup)
    assert not verdict.ok and verdict.reason == reason
    assert reason in verify.REASONS


def test_unreadable_run_or_review_history_refuses():
    fake = verify_fake()
    fake.add("GET", f"/repos/{REPO}/actions/runs/99", {"message": "x"}, status=500)
    assert run_verify(fake).reason == "run-unreadable"
    fake = verify_fake()
    fake.add("GET", f"/repos/{REPO}/actions/runs/99/approvals", {"message": "x"}, status=403)
    assert run_verify(fake).reason == "history-unreadable"


def test_executor_lookup_fails_closed():
    with pytest.raises(KeyError):
        executors.run(parse_request(request_dict()), NOOP_CLASS, {}, executors={})
    report = executors.run(parse_request(request_dict()), NOOP_CLASS)
    assert report["executor"] == "noop" and report["environment"] == "approval-noop"
