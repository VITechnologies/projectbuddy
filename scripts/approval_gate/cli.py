"""Command line for requesters, reviewers, and the gate workflow itself.

Every command prints one JSON document on stdout for agents and scripts, and
human progress on stderr. Exit codes: 0 approved and executed, 10 rejected,
11 expired, 12 failed, 20 pending, 3 refused, 2 invalid, 1 error; readiness
returns 0 enforcing, 21 advisory, 22 not ready, 23 unsupported.

There is deliberately no command to approve or reject: decisions are made by
a human in GitHub's review dialog, never by a token this tool holds.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Optional, TextIO

from . import DEFAULT_WORKFLOW, TOOL_VERSION, auth, executors, preflight, readiness, record, submit
from .checks import check_content, check_precondition, is_eligible_reviewer, issue_last_edited, read_environment, resolve_ref
from .github import API_URL, GitHubClient, GitHubError, Transport
from .leakscan import readable_text, scan
from .model import REQUEST_ID_RE, RequestError, format_time, parse_request
from .registry import RegistryError, default_registry, load_registry
from .verify import read_reviews, verify

READINESS_EXIT = {"enforcing": 0, "advisory": 21, "not-ready": 22, "unsupported": 23}


class Context:
    def __init__(self, environ: Mapping[str, str], transport: Optional[Transport], runner: auth.Runner,
                 clock: Callable[[], datetime], sleep: Callable[[float], None], stdout: TextIO, stderr: TextIO) -> None:
        self.environ = environ
        self.transport = transport
        self.runner = runner
        self.clock = clock
        self.sleep = sleep
        self.stdout = stdout
        self.stderr = stderr

    def client(self, token: Optional[str] = None) -> GitHubClient:
        return GitHubClient(token, transport=self.transport, api_url=self.environ.get("GITHUB_API_URL", API_URL))

    def emit(self, data: object) -> None:
        self.stdout.write(json.dumps(data, indent=2, sort_keys=True) + "\n")

    def say(self, message: str) -> None:
        self.stderr.write(message + "\n")

    @property
    def private_patterns(self) -> str:
        return self.environ.get("APPROVAL_GATE_PRIVATE_PATTERNS", "")


class Refused(Exception):
    def __init__(self, message: str, exit_code: int = submit.EXIT_REFUSED, data: Optional[dict] = None) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.data = data or {}


def _read_local(path: str) -> bytes:
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        raise Refused(f"cannot read {path}: {exc.strerror or exc}", submit.EXIT_INVALID) from exc


def _read_local_text(path: str) -> str:
    try:
        return _read_local(path).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Refused(f"{path} is not UTF-8 text", submit.EXIT_INVALID) from exc


def _text(value: Optional[str]) -> Optional[str]:
    """`@path` reads the text from a file so long rationale stays out of shell history."""
    if value is not None and value.startswith("@"):
        return _read_local_text(value[1:])
    return value


def _repo(ctx: Context, args: argparse.Namespace) -> str:
    repository = getattr(args, "repo", None) or ctx.environ.get("GITHUB_REPOSITORY")
    if not repository:
        raise Refused("name the gate repository with --repo OWNER/NAME", submit.EXIT_INVALID)
    return repository


def _login(ctx: Context, repositories: "list[str]") -> "tuple[GitHubClient, auth.Identity]":
    return auth.authenticate(ctx.client(), environ=ctx.environ, repositories=repositories, now=ctx.clock(), runner=ctx.runner)


# --- requester and reviewer commands -------------------------------------------

def cmd_whoami(ctx: Context, args: argparse.Namespace) -> int:
    repository = getattr(args, "repo", None) or ctx.environ.get("GITHUB_REPOSITORY")
    _, identity = _login(ctx, [repository] if repository else [])
    ctx.emit({"tool_version": TOOL_VERSION, "identity": identity.to_dict()})
    return 0


def cmd_classes(ctx: Context, args: argparse.Namespace) -> int:
    if args.registry:
        registry = load_registry(Path(args.registry))
        source = args.registry
    else:
        repository = _repo(ctx, args)
        gh, _ = _login(ctx, [repository])
        branch = gh.get(f"/repos/{repository}")["default_branch"]
        registry = submit.load_remote_registry(gh, repository, branch)
        source = f"{repository}@{branch}"
    ctx.emit({"source": source, "classes": {name: c.to_dict() for name, c in sorted(registry.items())}})
    return 0


def cmd_validate(ctx: Context, args: argparse.Namespace) -> int:
    repository = _repo(ctx, args)
    raw = sys.stdin.read() if args.request == "-" else _read_local_text(args.request)
    registry = load_registry(Path(args.registry)) if args.registry else default_registry()
    default = args.default_branch or ctx.client().get(f"/repos/{repository}")["default_branch"]
    report = preflight.offline_checks(raw, registry=registry, self_repository=repository, default_branch=default,
                                      now=ctx.clock(), private_patterns=ctx.private_patterns)
    ctx.emit(_report_json(report))
    return 0 if report.ok else submit.EXIT_INVALID


def _report_json(report: preflight.PreflightReport) -> dict:
    withheld = report.leak_blocked
    return {
        "ok": report.ok,
        "request_id": None if withheld or report.request is None else report.request.id,
        "request_digest": None if report.request is None else report.request.digest(),
        "checks": [{"id": c.id, "ok": c.ok,
                    "detail": c.detail if not withheld or c.id.startswith("leak-scan") else "withheld"}
                   for c in report.checks],
    }


def cmd_readiness(ctx: Context, args: argparse.Namespace) -> int:
    repository = _repo(ctx, args)
    gh, identity = _login(ctx, [repository])
    branch = gh.get(f"/repos/{repository}")["default_branch"]
    registry = submit.load_remote_registry(gh, repository, branch)
    if args.action_class and args.action_class not in registry:
        raise Refused(f"unknown action class {args.action_class!r}", submit.EXIT_INVALID)
    result = readiness.check(gh, repository=repository, registry=registry, identity=identity, only=args.action_class)
    for item in result["classes"]:
        ctx.say(f"{item['name']}: {item['status']}")
        for finding in item["findings"]:
            ctx.say(f"  - {finding['code']}: {finding['message']}")
    ctx.emit(result)
    return READINESS_EXIT[result["status"]]


def _content(ctx: Context, args: argparse.Namespace, request_id: str) -> "tuple[dict, dict[str, bytes]]":
    ref = f"{submit.STAGING_BRANCH_PREFIX}{request_id}"
    if args.commit:
        return {"kind": "commit", "value": args.commit}, {}
    if args.stage:
        files = {path: _read_local(path) for path in args.stage}
        # The real SHA exists only after staging; this placeholder never leaves the tool.
        return {"kind": "commit", "value": "0" * 40, "ref": ref}, files
    if args.stage_file:
        data = _read_local(args.stage_file)
        return {"kind": "sha256", "value": submit.sha256_hex(data), "ref": ref, "path": args.stage_file}, {args.stage_file: data}
    return {"kind": "sha256", "value": args.digest}, {}


def _build_request(ctx: Context, args: argparse.Namespace, gh: GitHubClient, repository: str, default: str,
                   registry: dict, login: str) -> "tuple[dict, dict[str, bytes]]":
    if args.request:
        try:
            data = json.loads(_read_local_text(args.request))
        except json.JSONDecodeError as exc:
            raise Refused(f"{args.request} is not valid JSON: {exc.msg}", submit.EXIT_INVALID) from exc
        if not isinstance(data, dict):
            raise Refused("the request file must hold a JSON object", submit.EXIT_INVALID)
        if str(data.get("requester", "")).lower() != login.lower():
            raise Refused(f"the request names requester {data.get('requester')!r}, but this tool is authenticated as {login}")
        return data, {}
    missing = [flag for flag, value in (("--id", args.id), ("--class", args.action_class), ("--rationale", args.rationale),
                                        ("--expected-effect", args.expected_effect), ("--rollback", args.rollback),
                                        ("--tracking-issue", args.tracking_issue)) if not value]
    if not any((args.commit, args.stage, args.stage_file, args.digest)):
        missing.append("one of --commit, --stage, --stage-file, --digest")
    if missing:
        raise Refused("missing " + ", ".join(missing), submit.EXIT_INVALID)
    action_class = registry.get(args.action_class)
    if action_class is None:
        raise Refused(f"unknown action class {args.action_class!r}; known: {', '.join(sorted(registry))}", submit.EXIT_INVALID)
    target_repository = args.target_repo or (repository if action_class.targets == ("@self",) else None)
    target_ref = args.target_ref or (f"refs/heads/{default}" if action_class.refs == ("@default",) else None)
    if not target_repository or not target_ref:
        raise Refused("this action class needs --target-repo and --target-ref", submit.EXIT_INVALID)
    now = ctx.clock()
    if args.precondition_issue:
        issue_repository = args.precondition_repo or repository
        edited = issue_last_edited(gh, issue_repository, args.precondition_issue)
        precondition = {"kind": "issue-edited", "repository": issue_repository, "issue": args.precondition_issue,
                        "last_edited_at": edited}
    else:
        sha = args.precondition_sha or resolve_ref(gh, target_repository, target_ref)
        if not sha:
            raise Refused(f"{target_repository} {target_ref} does not exist", submit.EXIT_INVALID)
        precondition = {"kind": "ref-sha", "repository": target_repository, "ref": target_ref, "sha": sha}
    content, files = _content(ctx, args, args.id)
    data = {
        "schema": "approval-gate/request@1",
        "id": args.id,
        "action_class": args.action_class,
        "target": {"repository": target_repository, "ref": target_ref},
        "content": content,
        "precondition": precondition,
        "rationale": _text(args.rationale),
        "expected_effect": _text(args.expected_effect),
        "rollback": _text(args.rollback),
        "requested_at": format_time(now),
        "expires_at": format_time(submit.default_expiry(now, action_class, args.expires_in)),
        "tracking_issue": args.tracking_issue,
        "requester": login,
    }
    return data, files


def cmd_submit(ctx: Context, args: argparse.Namespace) -> int:
    repository = _repo(ctx, args)
    repositories = [repository] + ([args.target_repo] if args.target_repo and args.target_repo != repository else [])
    gh, identity = _login(ctx, repositories)
    if not identity.login:
        raise Refused("the requester identity is unknown, so the tool cannot confirm it is not a reviewer; "
                      "authenticate as a user or GitHub App, or set APPROVAL_GATE_REQUESTER")
    default = gh.get(f"/repos/{repository}")["default_branch"]
    registry = submit.load_remote_registry(gh, repository, default)
    data, files = _build_request(ctx, args, gh, repository, default, registry, identity.login)

    report = preflight.offline_checks(data, registry=registry, self_repository=repository, default_branch=default,
                                      now=ctx.clock(), private_patterns=ctx.private_patterns)
    problems = submit.check_stage_paths(sorted(files))
    findings = scan([(f"staged path {i}", p) for i, p in enumerate(sorted(files), 1)]
                    + [(f"staged file {i}", readable_text(files[p])) for i, p in enumerate(sorted(files), 1)], ctx.private_patterns)
    if problems:
        report.add("staged-paths", False, "; ".join(problems))
    if findings:
        report.add("leak-scan-content", False, "blocked: " + "; ".join(f.describe() for f in findings))
    if not report.ok:
        raise Refused("the request is invalid", submit.EXIT_INVALID, {"preflight": _report_json(report)})
    request, action_class = report.request, report.action_class

    protection = read_environment(gh, repository, action_class.environment)
    if protection.problems():
        raise Refused("the approval environment is not protected: "
                      + "; ".join(message for _, message in protection.problems()) + "; run `readiness` for fixes")
    eligible = is_eligible_reviewer(gh, protection, identity.login)
    if eligible is not False:
        raise Refused(f"{identity.login} is an eligible reviewer of {action_class.environment}" if eligible
                      else f"cannot confirm that {identity.login} is not a reviewer of {action_class.environment}")
    if args.require_enforcing:
        result = readiness.check(gh, repository=repository, registry=registry, identity=identity, only=action_class.name)
        if result["status"] != "enforcing":
            raise Refused(f"class {action_class.name} is {result['status']}, not enforcing", data={"readiness": result})
    precondition = check_precondition(gh, request.precondition)
    if not precondition.ok:
        raise Refused(f"the target already drifted: {precondition.detail}")

    existing = submit.find_runs(gh, repository, args.workflow, request.id)
    if existing:
        # Request IDs are single-use: following the old run could report its outcome for different content.
        run_id = int(existing[0]["id"])
        raise Refused(f"request ID {request.id} was already used by run {run_id}; follow it with "
                      f"`status {request.id}`, or submit new content under a new ID",
                      data={"run_id": run_id, "run_url": existing[0].get("html_url")})
    if args.dry_run:
        ctx.emit({"dry_run": True, "request": data, "preflight": _report_json(report)})
        return 0

    if files:
        commit = submit.stage(gh, repository, request.id, files)
        if data["content"]["kind"] == "commit":
            data["content"]["value"] = commit
        request = parse_request(data)
        ctx.say(f"staged {len(files)} file(s) on {submit.STAGING_BRANCH_PREFIX}{request.id} at {commit}")
    content = check_content(gh, request, gate_repository=repository, allow_digest_only=action_class.allow_digest_only)
    if not content.ok:
        raise Refused(f"content check failed: {content.detail}")

    run_id = submit.dispatch(gh, repository, args.workflow, default, request, sleep=ctx.sleep)
    ctx.say(f"submitted {request.id} as run {run_id} (digest {request.digest()})")
    # Once the run waits at the gate, GitHub says whether this identity could approve it.
    current = submit.status(gh, repository, run_id, issue=request.tracking_issue)
    waited = 0.0
    while current.data["state"] == "running" and waited < args.confirm_timeout:
        step = min(10.0, args.confirm_timeout - waited)
        ctx.sleep(step)
        waited += step
        current = submit.status(gh, repository, run_id, issue=request.tracking_issue)
    if any(p["current_user_can_approve"] for p in current.data["pending"]):
        submit.withdraw(gh, repository, run_id)
        raise Refused(f"{identity.login} could approve its own request, so run {run_id} was withdrawn")
    if current.data["state"] == "running":
        ctx.say(f"run {run_id} has not reached the review gate yet, so self-approval was not confirmed here; "
                "the gated job still refuses a self-approved run")
    return _follow(ctx, args, gh, repository, run_id, request.tracking_issue)


def _follow(ctx: Context, args: argparse.Namespace, gh: GitHubClient, repository: str, run_id: int,
            issue: Optional[int]) -> int:
    if getattr(args, "wait", False):
        result = submit.wait(gh, repository, run_id, issue=issue, sleep=ctx.sleep, clock=ctx.clock,
                             timeout=args.timeout, interval=args.interval,
                             on_change=lambda s: ctx.say(f"run {run_id}: {s.data['state']}"))
    else:
        result = submit.status(gh, repository, run_id, issue=issue)
    ctx.emit(result.data)
    return result.exit_code


def _run_id(ctx: Context, args: argparse.Namespace, gh: GitHubClient, repository: str) -> int:
    if args.run:
        return args.run
    if not args.request_id:
        raise Refused("give a request ID or --run RUN_ID", submit.EXIT_INVALID)
    runs = submit.find_runs(gh, repository, args.workflow, args.request_id)
    if not runs:
        raise Refused(f"no run found for request {args.request_id}", submit.EXIT_ERROR)
    return int(runs[0]["id"])


def cmd_status(ctx: Context, args: argparse.Namespace) -> int:
    repository = _repo(ctx, args)
    gh, _ = _login(ctx, [repository])
    return _follow(ctx, args, gh, repository, _run_id(ctx, args, gh, repository), None)


def cmd_withdraw(ctx: Context, args: argparse.Namespace) -> int:
    repository = _repo(ctx, args)
    gh, _ = _login(ctx, [repository])
    run_id = _run_id(ctx, args, gh, repository)
    current = submit.status(gh, repository, run_id)
    if not current.pending:
        raise Refused(f"run {run_id} already completed", data=current.data)
    submit.withdraw(gh, repository, run_id)
    ctx.emit({"withdrawn": True, "run_id": run_id, "request_id": current.data["request_id"]})
    return 0


def cmd_queue(ctx: Context, args: argparse.Namespace) -> int:
    repositories = args.repo or ([ctx.environ["GITHUB_REPOSITORY"]] if ctx.environ.get("GITHUB_REPOSITORY") else [])
    if not repositories:
        raise Refused("name at least one repository with --repo", submit.EXIT_INVALID)
    gh, identity = _login(ctx, repositories)
    items = submit.queue(gh, repositories)
    for item in items:
        ctx.say(f"{item['expires_at']}  {item['repository']}  {item['request_id']}  "
                f"{'you can review' if item['can_review'] else 'not yours to review'}  {item['run_url']}")
    ctx.emit({"reviewer": identity.to_dict(), "waiting": items})
    return 0


def cmd_sweep(ctx: Context, args: argparse.Namespace) -> int:
    repository = _repo(ctx, args)
    gh, _ = _login(ctx, [repository])
    swept = submit.sweep(gh, repository, now=ctx.clock(), dry_run=args.dry_run)
    ctx.emit({"repository": repository, "dry_run": args.dry_run, "expired": swept})
    return 0


# --- gate workflow commands ------------------------------------------------------

def _ci(ctx: Context) -> "tuple[GitHubClient, str, int]":
    for name in ("GITHUB_REPOSITORY", "GITHUB_RUN_ID", "GITHUB_TOKEN"):
        if not ctx.environ.get(name):
            raise Refused(f"{name} is not set; this command runs inside the gate workflow", submit.EXIT_INVALID)
    return ctx.client(ctx.environ["GITHUB_TOKEN"]), ctx.environ["GITHUB_REPOSITORY"], int(ctx.environ["GITHUB_RUN_ID"])


def _append(ctx: Context, variable: str, text: str) -> None:
    path = ctx.environ.get(variable)
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)


def _outputs(ctx: Context, values: "dict[str, str]") -> None:
    _append(ctx, "GITHUB_OUTPUT", "".join(f"{name}={value}\n" for name, value in values.items()))


def _ci_registry(ctx: Context) -> dict:
    path = ctx.environ.get("APPROVAL_GATE_REGISTRY")
    return load_registry(Path(path)) if path else default_registry()


def cmd_preflight(ctx: Context, args: argparse.Namespace) -> int:
    gh, repository, run_id = _ci(ctx)
    env = ctx.environ
    report = preflight.PreflightReport()
    try:
        registry = _ci_registry(ctx)
    except RegistryError as exc:
        registry = None
        report.add("registry", False, str(exc))
    if registry is not None:
        default = gh.get(f"/repos/{repository}")["default_branch"]
        run = gh.get(f"/repos/{repository}/actions/runs/{run_id}")
        inputs = {name: env.get(f"APPROVAL_GATE_INPUT_{name.upper().replace('-', '_')}", "")
                  for name in ("request-id", "action-class", "tracking-issue", "expires-at")}
        report = preflight.offline_checks(env.get("APPROVAL_GATE_REQUEST", ""), registry=registry,
                                          self_repository=repository, default_branch=default, now=ctx.clock(),
                                          private_patterns=ctx.private_patterns, inputs=inputs)
        preflight.online_checks(report, gh, gate_repository=repository,
                                run_actor=(run.get("triggering_actor") or {}).get("login") or "",
                                run_head_branch=run.get("head_branch") or "", default_branch=default,
                                private_patterns=ctx.private_patterns,
                                probe_secret_visible=bool(env.get(executors.PROBE_SECRET)))
    summary = preflight.render_summary(report, gate_repository=repository,
                                       server_url=env.get("GITHUB_SERVER_URL", "https://github.com"),
                                       request_id_input=env.get("APPROVAL_GATE_INPUT_REQUEST_ID", ""))
    _append(ctx, "GITHUB_STEP_SUMMARY", summary)
    outputs = {"ok": "true" if report.ok else "false"}
    if report.ok:
        outputs.update(environment=report.action_class.environment, digest=report.request.digest())
    _outputs(ctx, outputs)
    for check in report.failures():
        ctx.say(f"::error title=Preflight {check.id}::"
                + ("failed; details withheld" if report.leak_blocked and not check.id.startswith("leak-scan") else check.detail))
    return 0 if report.ok else 1


def cmd_execute(ctx: Context, args: argparse.Namespace) -> int:
    gh, repository, run_id = _ci(ctx)
    env = ctx.environ
    request = parse_request(env.get("APPROVAL_GATE_REQUEST", ""))
    action_class = _ci_registry(ctx)[request.action_class]
    default = gh.get(f"/repos/{repository}")["default_branch"]
    verdict = verify(gh, request, action_class, gate_repository=repository, run_id=run_id,
                     run_attempt=int(env.get("GITHUB_RUN_ATTEMPT", "0")),
                     job_environment=env.get("APPROVAL_GATE_JOB_ENVIRONMENT", ""), default_branch=default,
                     now=ctx.clock())
    result: dict = {"ok": verdict.ok, "reason": verdict.reason, "detail": verdict.detail,
                    "request_digest": request.digest(), "execution": None}
    if verdict.ok:
        try:
            result["execution"] = executors.run(request, action_class, env)
        except Exception as exc:  # the record must still say what happened
            result["execution"] = {"error": f"{type(exc).__name__}: {exc}"}
    _outputs(ctx, {"verdict": json.dumps(result, sort_keys=True)})
    heading = "executed" if verdict.ok and not (result["execution"] or {}).get("error") else f"refused: {verdict.reason or 'executor error'}"
    _append(ctx, "GITHUB_STEP_SUMMARY", f"## Approval request `{request.id}`: {heading}\n\n{verdict.detail}\n\n"
            + (f"```json\n{json.dumps(result['execution'], indent=2, sort_keys=True)}\n```\n" if result["execution"] else ""))
    if heading != "executed":
        ctx.say(f"::error title=Approval gate refused::{verdict.reason or 'executor error'}: {verdict.detail}")
        return 1
    return 0


def cmd_record(ctx: Context, args: argparse.Namespace) -> int:
    gh, repository, run_id = _ci(ctx)
    env = ctx.environ
    now = ctx.clock()
    try:
        request = parse_request(env.get("APPROVAL_GATE_REQUEST", ""))
    except RequestError:
        request = None
    request_id = request.id if request else env.get("APPROVAL_GATE_INPUT_REQUEST_ID", "")
    issue_input = env.get("APPROVAL_GATE_INPUT_TRACKING_ISSUE", "")
    issue = request.tracking_issue if request else (int(issue_input) if issue_input.isdigit() and int(issue_input) > 0 else None)
    if not REQUEST_ID_RE.match(request_id or "") or issue is None:
        _append(ctx, "GITHUB_STEP_SUMMARY", "## No decision record\n\nThe request ID or tracking issue input is invalid.\n")
        ctx.say("::warning title=No decision record::the request ID or tracking issue input is invalid")
        return 0
    try:
        action_class = _ci_registry(ctx).get(request.action_class) if request else None
    except RegistryError:
        action_class = None
    reviews = []
    if action_class is not None:
        try:
            reviews = read_reviews(gh, repository, run_id, action_class.environment)
        except GitHubError as exc:
            ctx.say(f"::warning title=Review history unreadable::{exc.api_message}")
    try:
        verdict = json.loads(env.get("APPROVAL_GATE_VERDICT", "") or "null")
    except ValueError:
        verdict = None
    if not isinstance(verdict, dict):
        verdict = None
    decision, outcome, detail = record.decide(
        request=request, request_id=request_id, preflight_ok=env.get("APPROVAL_GATE_PREFLIGHT_RESULT") == "success",
        execute_result=env.get("APPROVAL_GATE_EXECUTE_RESULT", ""), verdict=verdict, reviews=reviews, now=now)
    try:
        jobs = gh.paginate(f"/repos/{repository}/actions/runs/{run_id}/jobs", key="jobs", limit=100)
    except GitHubError:
        jobs = []
    run = record.RunContext(repository, run_id, int(env.get("GITHUB_RUN_ATTEMPT", "1")),
                            env.get("GITHUB_SERVER_URL", "https://github.com"))
    rec = record.build_record(request=request, request_id=request_id, action_class=action_class, decision=decision,
                              outcome=outcome, detail=detail, reviews=reviews, run=run, now=now,
                              decided_at=record.decided_at(jobs, decision),
                              execution=(verdict or {}).get("execution"), private_patterns=ctx.private_patterns)
    comment = record.upsert(gh, repository, issue, rec)
    _append(ctx, "GITHUB_STEP_SUMMARY", record.render_comment(rec))
    _outputs(ctx, {"decision": decision, "outcome": outcome, "record-url": (comment or {}).get("html_url", "")})
    ctx.say(f"recorded {decision} ({outcome}) for {rec['request_id']} on #{issue}")
    return 0


# --- parser ------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="Invoke-ApprovalGate.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("--version", action="version", version=TOOL_VERSION)
    commands = parser.add_subparsers(dest="command", required=True)

    def add(name: str, handler: Callable, help_text: str, repo: bool = True) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=help_text, description=help_text)
        if repo:
            sub.add_argument("--repo", help="gate repository OWNER/NAME (default: GITHUB_REPOSITORY)")
            sub.add_argument("--workflow", default=DEFAULT_WORKFLOW, help="request workflow file name")
        sub.set_defaults(handler=handler)
        return sub

    add("whoami", cmd_whoami, "Show the identity the tool authenticates as (never the token).")
    sub = add("classes", cmd_classes, "List the action classes from the gate repository's default branch.")
    sub.add_argument("--registry", help="read a local registry file instead")
    sub = add("validate", cmd_validate, "Run the offline preflight checks on a request document.")
    sub.add_argument("request", help="request JSON file, or - for stdin")
    sub.add_argument("--registry", help="local registry file (default: built-in no-op class only)")
    sub.add_argument("--default-branch", help="gate default branch (default: read from GitHub)")
    sub = add("readiness", cmd_readiness, "Report whether each action class is enforcing, advisory, not ready, or unsupported.")
    sub.add_argument("--class", dest="action_class", help="check one class")

    sub = add("submit", cmd_submit, "Validate, stage, and submit one request.")
    sub.add_argument("--request", help="complete request JSON file (otherwise built from the options below)")
    sub.add_argument("--id")
    sub.add_argument("--class", dest="action_class")
    sub.add_argument("--target-repo")
    sub.add_argument("--target-ref")
    content = sub.add_mutually_exclusive_group()
    content.add_argument("--commit", help="full commit SHA in the target repository")
    content.add_argument("--stage", nargs="+", metavar="PATH", help="stage these files as an orphan commit")
    content.add_argument("--stage-file", metavar="PATH", help="stage one file bound by its SHA-256")
    content.add_argument("--digest", help="SHA-256 of a payload that stays with the requester")
    sub.add_argument("--precondition-sha", help="expected target SHA (default: its current SHA)")
    sub.add_argument("--precondition-issue", type=int, help="bind to an issue body instead of the target SHA")
    sub.add_argument("--precondition-repo", help="repository of --precondition-issue (default: gate repository)")
    sub.add_argument("--rationale", help="text, or @file")
    sub.add_argument("--expected-effect", help="text, or @file")
    sub.add_argument("--rollback", help="text, or @file")
    sub.add_argument("--expires-in", type=int, metavar="HOURS")
    sub.add_argument("--tracking-issue", type=int)
    sub.add_argument("--require-enforcing", action="store_true", help="refuse unless readiness reports enforcing")
    sub.add_argument("--dry-run", action="store_true", help="check everything, then stop before staging or dispatch")
    sub.add_argument("--confirm-timeout", type=float, default=120.0,
                     help="seconds to wait for the review gate to confirm this identity cannot approve")
    _wait_options(sub)

    for name, handler, help_text in (("status", cmd_status, "Show a request's state and decision."),
                                     ("withdraw", cmd_withdraw, "Cancel a request that is still waiting.")):
        sub = add(name, handler, help_text)
        sub.add_argument("request_id", nargs="?")
        sub.add_argument("--run", type=int, help="run ID instead of a request ID")
        if name == "status":
            _wait_options(sub)

    sub = commands.add_parser("queue", help="List requests waiting for review across repositories.")
    sub.add_argument("--repo", action="append", help="repository to scan (repeatable)")
    sub.set_defaults(handler=cmd_queue)
    sub = add("sweep", cmd_sweep, "Cancel requests still waiting after their expiry.")
    sub.add_argument("--dry-run", action="store_true")

    for name, handler in (("preflight", cmd_preflight), ("execute", cmd_execute), ("record", cmd_record)):
        add(name, handler, f"Gate workflow step: {name}. Reads its inputs from the environment.", repo=False)
    return parser


def _wait_options(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--wait", action="store_true", help="wait for the decision")
    sub.add_argument("--timeout", type=float, default=3600.0, help="seconds to wait (default 3600)")
    sub.add_argument("--interval", type=float, default=30.0, help="seconds between polls (default 30)")


def main(
    argv: Optional["list[str]"] = None,
    *,
    environ: Optional[Mapping[str, str]] = None,
    transport: Optional[Transport] = None,
    runner: Optional[auth.Runner] = None,
    clock: Optional[Callable[[], datetime]] = None,
    sleep: Optional[Callable[[float], None]] = None,
    stdout: Optional[TextIO] = None,
    stderr: Optional[TextIO] = None,
) -> int:
    args = build_parser().parse_args(argv)
    ctx = Context(os.environ if environ is None else environ, transport, runner or auth.subprocess_runner,
                  clock or (lambda: datetime.now(timezone.utc)), sleep or time.sleep,
                  stdout or sys.stdout, stderr or sys.stderr)
    try:
        return args.handler(ctx, args)
    except Refused as exc:
        ctx.say(f"refused: {exc}")
        ctx.emit({"refused": str(exc), "exit_code": exc.exit_code, **exc.data})
        return exc.exit_code
    except (RegistryError, RequestError, auth.AuthError, submit.SubmitError) as exc:
        code = getattr(exc, "exit_code", submit.EXIT_INVALID if isinstance(exc, (RegistryError, RequestError)) else submit.EXIT_ERROR)
        ctx.say(f"error: {exc}")
        ctx.emit({"error": str(exc), "exit_code": code})
        return code
    except GitHubError as exc:
        ctx.say(f"error: {exc}")
        ctx.emit({"error": str(exc), "status": exc.status, "exit_code": submit.EXIT_ERROR})
        return submit.EXIT_ERROR
