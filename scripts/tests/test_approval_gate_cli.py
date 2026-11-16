"""Approval gate: submission, run tracking, and every command of Invoke-ApprovalGate.py."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import runpy
import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from approval_gate_fakes import (  # noqa: E402
    DIGEST, NOW, REPO, REQUESTER, REVIEWER, SHA_A, SHA_B, FakeGitHub, environment, file_content, gate_repo,
    ref_route, request_dict, run_payload,
)

import approval_gate  # noqa: E402
from approval_gate import cli, executors, record, submit  # noqa: E402
from approval_gate.github import GitHubClient  # noqa: E402
from approval_gate.model import parse_request  # noqa: E402
from approval_gate.registry import NOOP_CLASS, RegistryError  # noqa: E402

ENV_PATH = f"/repos/{REPO}/environments/approval-noop"
WORKFLOW_RUNS = f"/repos/{REPO}/actions/workflows/approval-request.yml/runs"
DISPATCH = f"/repos/{REPO}/actions/workflows/approval-request.yml/dispatches"
RUN = f"/repos/{REPO}/actions/runs/99"
PENDING = f"{RUN}/pending_deployments"
COMMENTS = f"/repos/{REPO}/issues/7/comments"
REGISTRY_FILE = f"/repos/{REPO}/contents/.github/approval-gate/classes.json"
TITLE = "Approval request req-001 · noop · #7 · expires 2026-11-11T11:00:00Z"
REQUEST = parse_request(request_dict())


def client(fake: FakeGitHub) -> GitHubClient:
    return GitHubClient("token", transport=fake)


class Timeline:
    """Injected clock and sleep: sleeping advances the clock."""

    def __init__(self) -> None:
        self.now = NOW
        self.slept: "list[float]" = []

    def clock(self):
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)


def pending(can_approve: bool = False) -> list:
    return [{"environment": {"name": "approval-noop"}, "current_user_can_approve": can_approve,
             "reviewers": [{"type": "User", "reviewer": {"login": REVIEWER}}, {"type": "Team", "reviewer": {"slug": "t"}}]}]


def record_comment(decision="approved", outcome="executed", run_id=99) -> dict:
    rec = record.build_record(request=REQUEST, request_id="req-001", action_class=NOOP_CLASS, decision=decision,
                              outcome=outcome, detail="done", reviews=[], run=record.RunContext(REPO, run_id, 1),
                              now=NOW, decided_at=None, execution=None)
    return {"id": 5, "user": {"login": "github-actions[bot]"}, "body": record.render_comment(rec),
            "html_url": "https://github.com/octo-org/gate/issues/7#issuecomment-5"}


# --- submit module ----------------------------------------------------------------------

def test_run_titles_round_trip_and_match_the_workflow_run_name():
    assert submit.run_title(REQUEST) == TITLE
    assert submit.parse_title(TITLE) == {"id": "req-001", "action_class": "noop", "issue": 7,
                                         "expires": "2026-11-11T11:00:00Z"}
    assert submit.parse_title("Approval request Bad · noop · #7 · expires x") is None
    workflow = (Path(__file__).resolve().parents[2] / ".github" / "workflows" / "approval-request.yml").read_text()
    run_name = next(line for line in workflow.splitlines() if line.startswith("run-name:"))
    rendered = (run_name.split(":", 1)[1].strip().strip('"')
                .replace("${{ inputs.request-id }}", "req-001").replace("${{ inputs.action-class }}", "noop")
                .replace("${{ inputs.tracking-issue }}", "7").replace("${{ inputs.expires-at }}", "2026-11-11T11:00:00Z"))
    assert rendered == TITLE
    inputs = submit.dispatch_inputs(REQUEST)
    assert parse_request(inputs["request"]) == REQUEST and inputs["tracking-issue"] == "7"


def test_remote_registry_defaults_parses_and_rejects_corruption():
    assert submit.load_remote_registry(client(FakeGitHub()), REPO, "main") == {"noop": NOOP_CLASS}
    doc = {"schema": "approval-gate/classes@1", "classes": {"noop": {"executor": "noop", "environment": "approval-noop",
                                                                       "targets": ["@self"], "refs": ["@default"]}}}
    fake = FakeGitHub().add("GET", REGISTRY_FILE, file_content(json.dumps(doc).encode()))
    assert submit.load_remote_registry(client(fake), REPO, "main")["noop"].allow_digest_only is False
    assert fake.calls[0].query == {"ref": "main"}
    fake.add("GET", REGISTRY_FILE, file_content(b"{"))
    with pytest.raises(RegistryError):
        submit.load_remote_registry(client(fake), REPO, "main")


def test_stage_paths_must_be_plain_relative_non_workflow_files():
    problems = submit.check_stage_paths(["./a.txt", "/abs", ".github/workflows/x.yml", "ok.txt", "ok.txt"])
    assert len(problems) == 4
    assert "workflow file" in problems[2] and "unique" in problems[3]
    for path in ("../x", "a/../b", "a//b", ".GitHub/Workflows/x.yml", ".git/config", "sub/.GIT/hooks/post-checkout"):
        assert submit.check_stage_paths([path]), path
    assert submit.check_stage_paths([".github/approval-gate/notes.md", ".gitignore", "a.git/b"]) == []


def stage_fake(existing_tree=None) -> FakeGitHub:
    fake = FakeGitHub().add("POST", f"/repos/{REPO}/git/blobs", {"sha": "blob1"}, status=201)
    fake.add("POST", f"/repos/{REPO}/git/trees", {"sha": "tree1"}, status=201)
    fake.add("POST", f"/repos/{REPO}/git/commits", {"sha": SHA_B}, status=201)
    fake.add("POST", f"/repos/{REPO}/git/refs", {"ref": "refs/heads/requests/req-001"}, status=201)
    if existing_tree:
        ref_route(fake, REPO, "requests/req-001", SHA_A)
        fake.add("GET", f"/repos/{REPO}/git/commits/{SHA_A}", {"tree": {"sha": existing_tree}})
    return fake


def test_stage_creates_an_orphan_commit_and_never_overwrites():
    fake = stage_fake()
    assert submit.stage(client(fake), REPO, "req-001", {"b.txt": b"b", "a.txt": b"a"}) == SHA_B
    assert fake.called("POST", f"/repos/{REPO}/git/commits")[0].body["parents"] == []
    assert [e["path"] for e in fake.called("POST", f"/repos/{REPO}/git/trees")[0].body["tree"]] == ["a.txt", "b.txt"]
    assert fake.called("POST", f"/repos/{REPO}/git/refs")[0].body == {"ref": "refs/heads/requests/req-001", "sha": SHA_B}
    assert submit.stage(client(stage_fake(existing_tree="tree1")), REPO, "req-001", {"a.txt": b"a"}) == SHA_A
    with pytest.raises(submit.SubmitError) as caught:
        submit.stage(client(stage_fake(existing_tree="other")), REPO, "req-001", {"a.txt": b"a"})
    assert caught.value.exit_code == submit.EXIT_REFUSED
    for files in ({}, {".github/workflows/x.yml": b"on: push"}):
        with pytest.raises(submit.SubmitError) as caught:
            submit.stage(client(stage_fake()), REPO, "req-001", files)
        assert caught.value.exit_code == submit.EXIT_INVALID


def test_find_runs_matches_titles_newest_first():
    runs = [dict(run_payload(run_id=1), created_at="2026-11-10T01:00:00Z"),
            dict(run_payload(run_id=2), created_at="2026-11-10T02:00:00Z"),
            run_payload(run_id=3, title=TITLE.replace("req-001", "req-002")),
            {"id": 4, "display_title": "Unrelated"}]
    fake = FakeGitHub().add("GET", WORKFLOW_RUNS, {"workflow_runs": runs})
    assert [run["id"] for run in submit.find_runs(client(fake), REPO, "approval-request.yml", "req-001")] == [2, 1]
    assert fake.calls[0].query["event"] == "workflow_dispatch"


def test_dispatch_returns_the_run_or_correlates_by_title():
    timeline = Timeline()
    fake = FakeGitHub().add("POST", DISPATCH, {"workflow_run_id": 99, "run_url": "u"})
    assert submit.dispatch(client(fake), REPO, "approval-request.yml", "main", REQUEST, sleep=timeline.sleep) == 99
    body = fake.called("POST", DISPATCH)[0].body
    assert body["ref"] == "main" and body["return_run_details"] is True and body["inputs"]["request-id"] == "req-001"
    legacy = FakeGitHub().add("POST", DISPATCH, None, status=204)
    legacy.seq("GET", WORKFLOW_RUNS, [(200, {"workflow_runs": []}), (200, {"workflow_runs": [run_payload(run_id=77)]})])
    assert submit.dispatch(client(legacy), REPO, "approval-request.yml", "main", REQUEST, sleep=timeline.sleep) == 77
    lost = FakeGitHub().add("POST", DISPATCH, None, status=204).add("GET", WORKFLOW_RUNS, {"workflow_runs": []})
    with pytest.raises(submit.SubmitError, match="did not appear"):
        submit.dispatch(client(lost), REPO, "approval-request.yml", "main", REQUEST, sleep=timeline.sleep, attempts=2)


def test_status_reports_waiting_reviewers_and_self_approval_risk():
    fake = FakeGitHub().add("GET", RUN, run_payload(status="waiting")).add("GET", PENDING, pending(can_approve=True))
    result = submit.status(client(fake), REPO, 99)
    assert result.pending and result.exit_code == 20
    assert result.data["pending"][0]["reviewers"] == [REVIEWER, "t"]
    assert result.data["notes"] and result.data["request_id"] == "req-001" and result.data["tracking_issue"] == 7


@pytest.mark.parametrize("comments, expected", [
    ([record_comment()], (0, "executed")),
    ([record_comment("rejected", "refused:rejected")], (10, "refused:rejected")),
    ([record_comment("expired", "not-run:expired")], (11, "not-run:expired")),
    ([record_comment("approved", "refused:drift")], (12, "refused:drift")),
    ([record_comment(run_id=100)], (12, "record-missing")),
])
def test_completed_status_reads_the_decision_record(comments, expected):
    fake = FakeGitHub().add("GET", RUN, run_payload(status="completed", conclusion="success")).add("GET", COMMENTS, comments)
    result = submit.status(client(fake), REPO, 99)
    assert (result.exit_code, result.data["outcome"]) == expected
    assert submit.status(client(FakeGitHub().add("GET", RUN, run_payload(status="queued"))), REPO, 99).data["state"] == "running"


def test_completed_run_without_a_title_issue_has_no_record():
    fake = FakeGitHub().add("GET", RUN, run_payload(status="completed", title="Something else"))
    assert submit.status(client(fake), REPO, 99).data["outcome"] == "record-missing"


def test_wait_polls_until_completed_or_timeout():
    timeline = Timeline()
    fake = FakeGitHub().seq("GET", RUN, [(200, run_payload(status="queued")), (200, run_payload(status="waiting")),
                                         (200, run_payload(status="completed"))])
    fake.add("GET", PENDING, pending()).add("GET", COMMENTS, [record_comment()])
    states = []
    result = submit.wait(client(fake), REPO, 99, issue=7, sleep=timeline.sleep, clock=timeline.clock, timeout=600,
                         interval=30, on_change=lambda s: states.append(s.data["state"]))
    assert result.exit_code == 0 and states == ["running", "waiting", "completed"] and timeline.slept == [30, 30]
    stuck = FakeGitHub().add("GET", RUN, run_payload(status="queued"))
    result = submit.wait(client(stuck), REPO, 99, issue=7, sleep=timeline.sleep, clock=timeline.clock, timeout=60, interval=30)
    assert result.pending


def queue_fake() -> FakeGitHub:
    fake = FakeGitHub().add("GET", f"/repos/{REPO}/actions/runs", {"workflow_runs": [
        run_payload(run_id=99, status="waiting"),
        run_payload(run_id=98, status="waiting", title=TITLE.replace("req-001", "req-000").replace("2026-11-11", "2026-11-09")),
        {"id": 5, "display_title": "Deploy site"}]})
    fake.add("GET", PENDING, pending(can_approve=True)).add("GET", f"/repos/{REPO}/actions/runs/98/pending_deployments", [])
    fake.add("POST", f"/repos/{REPO}/actions/runs/98/cancel", {}, status=202)
    return fake


def test_queue_lists_waiting_requests_soonest_expiry_first():
    fake = queue_fake()
    items = submit.queue(client(fake), [REPO])
    assert [item["request_id"] for item in items] == ["req-000", "req-001"]
    assert items[1]["can_review"] is True and items[1]["environments"] == ["approval-noop"]
    assert fake.calls[0].query["status"] == "waiting"


def test_sweep_cancels_only_expired_waiting_requests():
    fake = queue_fake()
    assert submit.sweep(client(fake), REPO, now=NOW, dry_run=True)[0]["cancelled"] is False
    assert not fake.called("POST", f"/repos/{REPO}/actions/runs/98/cancel")
    swept = submit.sweep(client(fake), REPO, now=NOW, dry_run=False)
    assert [s["request_id"] for s in swept] == ["req-000"] and swept[0]["cancelled"]
    assert len(fake.called("POST", f"/repos/{REPO}/actions/runs/98/cancel")) == 1
    assert submit.default_expiry(NOW, NOOP_CLASS, 5) == NOW + timedelta(hours=5)
    assert submit.default_expiry(NOW, NOOP_CLASS, None) == NOW + timedelta(hours=72)


# --- CLI harness ---------------------------------------------------------------------------

def run_cli(argv, fake, env=None, timeline=None, runner=None):
    timeline = timeline or Timeline()
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(argv, environ={"GH_TOKEN": "t"} if env is None else env, transport=fake,
                    runner=runner or (lambda argv, stdin: (1, b"")), clock=timeline.clock, sleep=timeline.sleep,
                    stdout=out, stderr=err)
    text = out.getvalue()
    return code, (json.loads(text) if text.strip() else None), err.getvalue()


def world(*, env=None, permissions=None) -> FakeGitHub:
    fake = FakeGitHub()
    gate_repo(fake, permissions=permissions if permissions is not None else {"push": False, "admin": False})
    fake.add("GET", "/user", {"login": REQUESTER, "type": "Bot"})
    fake.add("GET", ENV_PATH, env or environment())
    ref_route(fake, REPO, "main", SHA_A)
    fake.add("GET", WORKFLOW_RUNS, {"workflow_runs": []})
    fake.add("POST", DISPATCH, {"workflow_run_id": 99, "run_url": "u"})
    fake.add("GET", RUN, run_payload(status="waiting"))
    fake.add("GET", PENDING, pending())
    fake.add("POST", f"{RUN}/cancel", {}, status=202)
    return fake


SUBMIT = ["submit", "--repo", REPO, "--id", "req-001", "--class", "noop", "--digest", DIGEST,
          "--rationale", "Prove the gate.", "--expected-effect", "Nothing.", "--rollback", "Nothing.",
          "--tracking-issue", "7"]


def test_entry_script_runs_the_cli(monkeypatch, capsys):
    script = Path(__file__).resolve().parents[1] / "Invoke-ApprovalGate.py"
    monkeypatch.setattr(sys, "argv", [str(script), "--version"])
    with pytest.raises(SystemExit) as caught:
        runpy.run_path(str(script), run_name="__main__")
    assert caught.value.code == 0 and capsys.readouterr().out.strip() == approval_gate.TOOL_VERSION


# --- requester commands -------------------------------------------------------------------

def test_whoami_and_classes(tmp_path):
    code, out, _ = run_cli(["whoami"], world())
    assert code == 0 and out["identity"] == {"login": REQUESTER, "kind": "app", "source": "GH_TOKEN"}
    code, out, _ = run_cli(["classes", "--repo", REPO], world())
    assert code == 0 and out["source"] == f"{REPO}@main" and "noop" in out["classes"]
    bad = tmp_path / "classes.json"
    bad.write_text("{}", encoding="utf-8")
    code, out, err = run_cli(["classes", "--registry", str(bad)], world())
    assert code == 2 and "schema must be" in out["error"]
    code, out, _ = run_cli(["classes", "--registry", str(tmp_path / "none.json")], world())
    assert code == 0 and list(out["classes"]) == ["noop"]


def test_validate_reads_files_and_stdin(tmp_path, monkeypatch):
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request_dict()), encoding="utf-8")
    code, out, _ = run_cli(["validate", str(path), "--repo", REPO, "--default-branch", "main"], world(),
                           timeline=Timeline())
    assert code == 0 and out["ok"] and out["request_id"] == "req-001"
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request_dict(rationale="ghp_" + "A" * 36))))
    code, out, _ = run_cli(["validate", "-", "--repo", REPO], world())
    assert code == 2 and out["request_id"] is None
    assert {c["id"]: c["detail"] for c in out["checks"]}["target"] == "withheld"
    code, out, _ = run_cli(["validate", str(path)], world())
    assert code == 2 and "--repo" in out["refused"]


@pytest.mark.parametrize("permissions, env, expected", [
    ({"push": False}, None, 0),
    ({"push": True}, None, 21),
    ({"push": False}, environment(prevent_self_review=False), 22),
])
def test_readiness_exit_codes(permissions, env, expected):
    code, out, err = run_cli(["readiness", "--repo", REPO], world(permissions=permissions, env=env))
    assert code == expected and out["classes"][0]["name"] == "noop" and "noop:" in err


def test_readiness_unsupported_and_unknown_class():
    fake = world()
    gate_repo(fake, permissions={"push": False}, visibility="private", owner_type="User")
    assert run_cli(["readiness", "--repo", REPO], fake)[0] == 23
    assert run_cli(["readiness", "--repo", REPO, "--class", "deploy"], world())[0] == 2


def test_submit_dispatches_and_reports_the_pending_request():
    fake = world()
    timeline = Timeline()
    code, out, err = run_cli(SUBMIT, fake, timeline=timeline)
    assert code == 20 and out["state"] == "waiting" and out["request_id"] == "req-001", err
    inputs = fake.called("POST", DISPATCH)[0].body["inputs"]
    request = parse_request(inputs["request"])
    assert request.requester == REQUESTER and request.precondition.sha == SHA_A
    assert request.expires_at == NOW + timedelta(hours=72) and request.content.value == DIGEST
    assert "submitted req-001 as run 99" in err


def test_submit_waits_through_confirmation_to_the_decision():
    fake = world()
    fake.seq("GET", RUN, [(200, run_payload(status="queued")), (200, run_payload(status="waiting")),
                          (200, run_payload(status="waiting")), (200, run_payload(status="completed"))])
    fake.add("GET", COMMENTS, [record_comment()])
    timeline = Timeline()
    code, out, err = run_cli(SUBMIT + ["--wait", "--interval", "5"], fake, timeline=timeline)
    assert code == 0 and out["outcome"] == "executed" and out["record_url"].endswith("issuecomment-5"), err
    assert timeline.slept[0] == 10.0 and "run 99: waiting" in err


def test_submit_withdraws_when_the_requester_could_approve():
    fake = world()
    fake.add("GET", PENDING, pending(can_approve=True))
    code, out, _ = run_cli(SUBMIT, fake)
    assert code == 3 and "withdrawn" in out["refused"] and fake.called("POST", f"{RUN}/cancel")


def test_submit_says_when_self_approval_could_not_be_confirmed():
    fake = world()
    fake.add("GET", RUN, run_payload(status="queued"))
    code, out, err = run_cli(SUBMIT + ["--confirm-timeout", "0"], fake)
    assert code == 20 and out["state"] == "running" and "self-approval was not confirmed" in err
    assert not fake.called("POST", f"{RUN}/cancel")


@pytest.mark.parametrize("change, message", [
    (dict(env=environment(prevent_self_review=False)), "not protected"),
    (dict(env=environment(users=(REQUESTER,))), "is an eligible reviewer"),
    (dict(env=environment(users=(), teams=("maintainers",))), "cannot confirm"),
])
def test_submit_refuses_unprotected_or_self_reviewable_classes(change, message):
    code, out, _ = run_cli(SUBMIT, world(**change))
    assert code == 3 and message in out["refused"]


def test_submit_refuses_unknown_identity_drift_reuse_and_non_enforcing_classes():
    anonymous = world()
    anonymous.add("GET", "/user", {"message": "x"}, status=401)
    assert "identity is unknown" in run_cli(SUBMIT, anonymous, env={})[1]["refused"]

    drift = SUBMIT + ["--precondition-sha", SHA_B]
    assert "already drifted" in run_cli(drift, world())[1]["refused"]

    reused = world()
    reused.add("GET", WORKFLOW_RUNS, {"workflow_runs": [run_payload()]})
    code, out, _ = run_cli(SUBMIT, reused)
    assert code == 3 and out["run_id"] == 99 and "status req-001" in out["refused"]
    assert not reused.called("POST", DISPATCH)

    code, out, _ = run_cli(SUBMIT + ["--require-enforcing"], world(permissions={"push": True}))
    assert code == 3 and out["readiness"]["status"] == "advisory"
    assert run_cli(SUBMIT + ["--require-enforcing", "--dry-run"], world())[0] == 0


def test_submit_dry_run_stops_before_any_write():
    fake = world()
    code, out, _ = run_cli(SUBMIT + ["--dry-run"], fake)
    assert code == 0 and out["dry_run"] and out["preflight"]["ok"]
    assert not [call for call in fake.calls if call.method == "POST"]


@pytest.mark.parametrize("argv, message", [
    (["submit", "--repo", REPO, "--id", "req-001"], "missing --class"),
    (SUBMIT[:6] + ["deploy"] + SUBMIT[7:], "unknown action class"),
    ([a if a != "Prove the gate." else "token ghp_" + "A" * 36 for a in SUBMIT], "the request is invalid"),
    (SUBMIT + ["--expires-in", "100"], "the request is invalid"),
    (SUBMIT + ["--target-ref", "refs/heads/nowhere"], "does not exist"),
])
def test_submit_rejects_invalid_requests_before_anything_leaves(argv, message):
    fake = world()
    code, out, _ = run_cli(argv, fake)
    assert code == 2 and message in out["refused"]
    assert not [call for call in fake.calls if call.method == "POST"]


def test_submit_with_wildcard_class_needs_explicit_targets():
    fake = world()
    doc = {"schema": "approval-gate/classes@1", "classes": {"site": {
        "executor": "noop", "environment": "approval-site", "targets": ["octo-org/*"], "refs": ["refs/heads/*"]}}}
    fake.add("GET", REGISTRY_FILE, file_content(json.dumps(doc).encode()))
    argv = SUBMIT[:6] + ["site"] + SUBMIT[7:]
    assert "--target-repo and --target-ref" in run_cli(argv, fake)[1]["refused"]


def test_submit_stages_files_and_binds_the_staged_commit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "plan.md").write_text("The plan.", encoding="utf-8")
    fake = world()
    for route, body in stage_fake().routes.items():
        fake.routes[route] = body
    fake.seq("GET", f"/repos/{REPO}/git/ref/heads/requests/req-001", [(404, {"message": "Not Found"}),
                                                                      (200, {"object": {"sha": SHA_B}})])
    argv = [a for a in SUBMIT if a not in ("--digest", DIGEST)] + ["--stage", "notes/plan.md"]
    code, out, err = run_cli(argv, fake)
    assert code == 20, (out, err)
    request = parse_request(fake.called("POST", DISPATCH)[0].body["inputs"]["request"])
    assert request.content.to_dict() == {"kind": "commit", "value": SHA_B, "ref": "requests/req-001"}
    assert "staged 1 file(s)" in err


def test_submit_stage_file_binds_its_sha256_and_checks_it(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "payload.json").write_bytes(b'{"a": 1}')
    fake = world()
    for route, body in stage_fake().routes.items():
        fake.routes[route] = body
    fake.add("GET", f"/repos/{REPO}/contents/payload.json", file_content(b"tampered"))
    argv = [a for a in SUBMIT if a not in ("--digest", DIGEST)] + ["--stage-file", "payload.json"]
    code, out, _ = run_cli(argv, fake)
    assert code == 3 and "content check failed" in out["refused"]
    assert not fake.called("POST", DISPATCH)
    fake.add("GET", f"/repos/{REPO}/contents/payload.json", file_content(b'{"a": 1}'))
    fake.add("GET", WORKFLOW_RUNS, {"workflow_runs": []})
    assert run_cli(argv, fake)[0] == 20
    request = parse_request(fake.called("POST", DISPATCH)[0].body["inputs"]["request"])
    assert request.content.value == hashlib.sha256(b'{"a": 1}').hexdigest() and request.content.path == "payload.json"


def test_submit_refuses_leaking_staged_content(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.txt").write_bytes(b"key: AKIA" + b"B" * 16)
    (tmp_path / "b.bin").write_bytes(b"\xff\xfe")
    (tmp_path / "c").write_bytes(b"c")
    argv = [a for a in SUBMIT if a not in ("--digest", DIGEST)] + ["--stage", "a.txt", "b.bin", "./c"]
    code, out, _ = run_cli(argv, world())
    checks = {c["id"]: c for c in out["preflight"]["checks"]}
    assert code == 2 and "staged file 2: matched aws-access-key" in checks["leak-scan-content"]["detail"]
    assert checks["staged-paths"] == {"id": "staged-paths", "ok": False, "detail": "withheld"}
    assert "a.txt" not in json.dumps(out)


@pytest.mark.parametrize("data", [
    ("\ufeffgh" + "p_" + "Q" * 36).encode("utf-16-le"),
    b"caf\xe9 AKIA" + b"B" * 16,
], ids=["powershell-utf-16", "cp1252"])
def test_submit_scans_staged_files_that_are_not_utf8(tmp_path, monkeypatch, data):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "out.txt").write_bytes(data)
    fake = world()
    argv = [a for a in SUBMIT if a not in ("--digest", DIGEST)] + ["--stage", "out.txt"]
    code, out, _ = run_cli(argv, fake)
    checks = {c["id"]: c for c in out["preflight"]["checks"]}
    assert code == 2 and "staged file 1: matched" in checks["leak-scan-content"]["detail"]
    assert not any(call.method != "GET" for call in fake.calls)


def test_submit_binds_an_existing_commit():
    fake = world()
    at = SUBMIT.index("--digest")
    argv = SUBMIT[:at] + ["--commit", SHA_B] + SUBMIT[at + 2:]
    code, out, _ = run_cli(argv, fake)
    assert code == 3 and "not found" in out["refused"] and not fake.called("POST", DISPATCH)
    fake.add("GET", f"/repos/{REPO}/commits/{SHA_B}", {"sha": SHA_B})
    assert run_cli(argv, fake)[0] == 20
    content = parse_request(fake.called("POST", DISPATCH)[0].body["inputs"]["request"]).content
    assert (content.kind, content.value) == ("commit", SHA_B)


def test_submit_from_a_request_file_and_an_issue_precondition(tmp_path):
    fake = world()
    fake.add("POST", "/graphql", {"data": {"repository": {"issue": {"lastEditedAt": None}}}})
    (tmp_path / "why.md").write_text("Because the plan says so.", encoding="utf-8")
    argv = [a if a != "Prove the gate." else f"@{tmp_path / 'why.md'}" for a in SUBMIT] + ["--precondition-issue", "3"]
    assert run_cli(argv, fake)[0] == 20
    request = parse_request(fake.called("POST", DISPATCH)[0].body["inputs"]["request"])
    assert request.rationale == "Because the plan says so." and request.precondition.kind == "issue-edited"

    path = tmp_path / "request.json"
    path.write_text(json.dumps(request_dict(requester="someone-else")), encoding="utf-8")
    code, out, _ = run_cli(["submit", "--repo", REPO, "--request", str(path)], world())
    assert code == 3 and "names requester" in out["refused"]
    path.write_text("[1]", encoding="utf-8")
    assert run_cli(["submit", "--repo", REPO, "--request", str(path)], world())[0] == 2
    data = request_dict(requested_at="2026-11-10T11:59:00Z")
    path.write_text(json.dumps(data), encoding="utf-8")
    fresh = world()
    assert run_cli(["submit", "--repo", REPO, "--request", str(path)], fresh)[0] == 20
    assert parse_request(fresh.called("POST", DISPATCH)[0].body["inputs"]["request"]).digest() == parse_request(data).digest()


# --- follow-up commands -----------------------------------------------------------------

def test_status_withdraw_queue_and_sweep():
    fake = world()
    fake.add("GET", WORKFLOW_RUNS, {"workflow_runs": [run_payload()]})
    code, out, _ = run_cli(["status", "req-001", "--repo", REPO], fake)
    assert code == 20 and out["run"]["id"] == 99
    assert run_cli(["status", "--run", "99", "--repo", REPO], fake)[0] == 20
    assert run_cli(["status", "--repo", REPO], fake)[0] == 2
    assert run_cli(["status", "req-404", "--repo", REPO], world())[0] == 1

    code, out, _ = run_cli(["withdraw", "req-001", "--repo", REPO], fake)
    assert code == 0 and out == {"withdrawn": True, "run_id": 99, "request_id": "req-001"}
    done = world()
    done.add("GET", RUN, run_payload(status="completed")).add("GET", COMMENTS, [record_comment()])
    code, out, _ = run_cli(["withdraw", "--run", "99", "--repo", REPO], done)
    assert code == 3 and out["outcome"] == "executed"

    fake = queue_fake()
    fake.add("GET", "/user", {"login": REVIEWER, "type": "User"})
    code, out, err = run_cli(["queue", "--repo", REPO], fake)
    assert code == 0 and len(out["waiting"]) == 2 and "you can review" in err
    assert run_cli(["queue"], fake, env={"GH_TOKEN": "t", "GITHUB_REPOSITORY": REPO})[0] == 0
    assert run_cli(["queue"], fake)[0] == 2
    code, out, _ = run_cli(["sweep", "--repo", REPO, "--dry-run"], fake)
    assert code == 0 and out["expired"][0]["request_id"] == "req-000"


def test_error_handlers_map_exit_codes():
    broken = world()
    broken.add("GET", f"/repos/{REPO}", {"message": "boom"}, status=500)
    code, out, _ = run_cli(["classes", "--repo", REPO], broken)
    assert code == 1 and out["status"] == 500
    code, out, _ = run_cli(["whoami"], world(), env={"APPROVAL_GATE_APP_ID": "1"})
    assert code == 1 and "both" in out["error"]


# --- gate workflow commands --------------------------------------------------------------

def ci_env(tmp_path, **extra) -> dict:
    env = {"GITHUB_REPOSITORY": REPO, "GITHUB_RUN_ID": "99", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_TOKEN": "t",
           "GITHUB_OUTPUT": str(tmp_path / "output"), "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
           "APPROVAL_GATE_REQUEST": REQUEST.canonical_json(), "APPROVAL_GATE_INPUT_REQUEST_ID": "req-001",
           "APPROVAL_GATE_INPUT_ACTION_CLASS": "noop", "APPROVAL_GATE_INPUT_TRACKING_ISSUE": "7",
           "APPROVAL_GATE_INPUT_EXPIRES_AT": "2026-11-11T11:00:00Z",
           "APPROVAL_GATE_REGISTRY": str(tmp_path / "caller" / "classes.json"),
           "APPROVAL_GATE_JOB_ENVIRONMENT": "approval-noop"}
    env.update(extra)
    return env


def outputs(tmp_path) -> dict:
    path = tmp_path / "output"
    if not path.exists():
        return {}
    return dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines())


def gate_world() -> FakeGitHub:
    fake = world()
    fake.add("GET", RUN, run_payload())
    fake.add("GET", f"{RUN}/approvals", [{"state": "approved", "comment": "Fine.", "user": {"login": REVIEWER},
                                         "environments": [{"name": "approval-noop"}]}])
    fake.add("GET", f"{RUN}/jobs", {"jobs": [{"name": "Gate / Execute approved request",
                                              "started_at": "2026-11-10T12:10:00Z", "completed_at": "2026-11-10T12:11:00Z"}]})
    fake.add("GET", COMMENTS, [])
    fake.add("POST", COMMENTS, {"id": 9, "html_url": "https://github.com/octo-org/gate/issues/7#issuecomment-9"}, status=201)
    return fake


def test_ci_commands_need_the_run_context(tmp_path):
    code, out, _ = run_cli(["preflight"], gate_world(), env={})
    assert code == 2 and "GITHUB_REPOSITORY" in out["refused"] and outputs(tmp_path) == {}


def test_preflight_publishes_the_summary_and_outputs(tmp_path):
    code, _, err = run_cli(["preflight"], gate_world(), env=ci_env(tmp_path))
    assert code == 0, err
    result = outputs(tmp_path)
    assert result == {"ok": "true", "environment": "approval-noop", "digest": REQUEST.digest()}
    summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "## Approval request `req-001`" in summary and "| `secret-isolation` | pass |" in summary


def test_preflight_failures_are_annotated_and_leaks_withheld(tmp_path):
    leaky = request_dict(rationale="see /home/alice/x")
    env = ci_env(tmp_path, APPROVAL_GATE_REQUEST=json.dumps(leaky), APPROVAL_GATE_PROBE="visible")
    code, _, err = run_cli(["preflight"], gate_world(), env=env)
    assert code == 1 and outputs(tmp_path) == {"ok": "false"}
    assert "::error title=Preflight leak-scan::blocked: rationale: matched local-path" in err
    assert "/home/alice" not in (tmp_path / "summary.md").read_text(encoding="utf-8")

    (tmp_path / "caller").mkdir()
    (tmp_path / "caller" / "classes.json").write_text("{", encoding="utf-8")
    code, _, err = run_cli(["preflight"], gate_world(), env=ci_env(tmp_path))
    assert code == 1 and "Preflight registry" in err


def test_execute_verifies_then_runs_the_executor(tmp_path):
    code, _, err = run_cli(["execute"], gate_world(), env=ci_env(tmp_path, APPROVAL_GATE_PROBE="set"))
    assert code == 0, err
    verdict = json.loads(outputs(tmp_path)["verdict"])
    assert verdict["ok"] and verdict["execution"]["writes"] == 0 and verdict["execution"]["probe_secret_present"]
    assert "executed" in (tmp_path / "summary.md").read_text(encoding="utf-8")


def test_execute_refuses_without_approval_and_reports_executor_errors(tmp_path, monkeypatch):
    fake = gate_world()
    fake.add("GET", f"{RUN}/approvals", [])
    code, _, err = run_cli(["execute"], fake, env=ci_env(tmp_path))
    assert code == 1 and "no-approval" in err
    assert json.loads(outputs(tmp_path)["verdict"])["reason"] == "no-approval"

    def broken(request, action_class, environ):
        raise RuntimeError("disk full")

    monkeypatch.setitem(executors.EXECUTORS, "noop", broken)
    other = tmp_path / "second"
    other.mkdir()
    code, _, err = run_cli(["execute"], gate_world(), env=ci_env(other))
    assert code == 1 and "executor error" in err
    assert json.loads(outputs(other)["verdict"])["execution"] == {"error": "RuntimeError: disk full"}
    assert run_cli(["execute"], gate_world(), env=ci_env(tmp_path, APPROVAL_GATE_REQUEST="{"))[0] == 2


def test_record_writes_the_decision_from_the_verdict(tmp_path):
    verdict = {"ok": True, "reason": None, "detail": "approved by alice", "execution": {"writes": 0}}
    env = ci_env(tmp_path, APPROVAL_GATE_PREFLIGHT_RESULT="success", APPROVAL_GATE_EXECUTE_RESULT="success",
                 APPROVAL_GATE_VERDICT=json.dumps(verdict))
    fake = gate_world()
    code, _, err = run_cli(["record"], fake, env=env)
    assert code == 0 and "recorded approved (executed) for req-001 on #7" in err
    assert outputs(tmp_path) == {"decision": "approved", "outcome": "executed",
                                 "record-url": "https://github.com/octo-org/gate/issues/7#issuecomment-9"}
    rec = record.parse_comment(fake.called("POST", COMMENTS)[0].body["body"])
    assert rec["decided_at"] == "2026-11-10T12:10:00Z" and rec["reviewers"][0]["comment"] == "Fine."


@pytest.mark.parametrize("extra, expected", [
    (dict(APPROVAL_GATE_PREFLIGHT_RESULT="failure", APPROVAL_GATE_EXECUTE_RESULT="skipped"), "not-run:preflight"),
    (dict(APPROVAL_GATE_PREFLIGHT_RESULT="success", APPROVAL_GATE_EXECUTE_RESULT="cancelled",
          APPROVAL_GATE_VERDICT="not json"), "not-run:withdrawn"),
    (dict(APPROVAL_GATE_PREFLIGHT_RESULT="success", APPROVAL_GATE_EXECUTE_RESULT="failure",
          APPROVAL_GATE_VERDICT="[1]"), "not-run:failure"),
    (dict(APPROVAL_GATE_REQUEST="{", APPROVAL_GATE_PREFLIGHT_RESULT="failure"), "not-run:preflight"),
])
def test_record_without_a_verdict(tmp_path, extra, expected):
    fake = gate_world()
    assert run_cli(["record"], fake, env=ci_env(tmp_path, **extra))[0] == 0
    assert outputs(tmp_path)["outcome"] == expected


def test_record_tolerates_unreadable_history_and_registry_errors(tmp_path):
    fake = gate_world()
    fake.add("GET", f"{RUN}/approvals", {"message": "x"}, status=403)
    fake.add("GET", f"{RUN}/jobs", {"message": "x"}, status=500)
    code, _, err = run_cli(["record"], fake, env=ci_env(tmp_path, APPROVAL_GATE_EXECUTE_RESULT="failure",
                                                        APPROVAL_GATE_PREFLIGHT_RESULT="success"))
    assert code == 0 and "Review history unreadable" in err
    (tmp_path / "caller").mkdir()
    (tmp_path / "caller" / "classes.json").write_text("{", encoding="utf-8")
    assert run_cli(["record"], gate_world(), env=ci_env(tmp_path))[0] == 0


def test_record_skips_invalid_inputs_and_withholds_leaks(tmp_path):
    code, _, err = run_cli(["record"], gate_world(), env=ci_env(tmp_path, APPROVAL_GATE_REQUEST="{",
                                                                APPROVAL_GATE_INPUT_TRACKING_ISSUE="x"))
    assert code == 0 and "No decision record" in err
    leaky = request_dict(target={"repository": "octo-org/internal-host7", "ref": "refs/heads/main"})
    fake = gate_world()
    env = ci_env(tmp_path, APPROVAL_GATE_REQUEST=json.dumps(leaky), APPROVAL_GATE_PREFLIGHT_RESULT="failure",
                 APPROVAL_GATE_PRIVATE_PATTERNS="internal-host\\d")
    assert run_cli(["record"], fake, env=env)[0] == 0
    body = fake.called("POST", COMMENTS)[0].body["body"]
    assert "internal-host7" not in body and "req-001" not in body
    assert base64.b64decode(record.DATA_RE.search(body).group(1))


def test_unreadable_local_inputs_are_refused_as_invalid(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "latin1.txt").write_bytes(b"caf\xe9")
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")
    without_digest = [a for a in SUBMIT if a not in ("--digest", DIGEST)]
    cases = [
        (without_digest + ["--stage", "missing.txt"], "cannot read missing.txt"),
        (without_digest + ["--stage-file", "missing.bin"], "cannot read missing.bin"),
        ([a if a != "Nothing." else "@missing.md" for a in SUBMIT], "cannot read missing.md"),
        ([a if a != "Nothing." else "@latin1.txt" for a in SUBMIT], "latin1.txt is not UTF-8"),
        (["submit", "--repo", REPO, "--request", "broken.json"], "broken.json is not valid JSON"),
        (["validate", "missing.json", "--repo", REPO], "cannot read missing.json"),
    ]
    for argv, message in cases:
        fake = world()
        code, out, _ = run_cli(argv, fake)
        assert code == 2 and message in out["refused"], (argv, out)
        assert not [call for call in fake.calls if call.method == "POST"]
