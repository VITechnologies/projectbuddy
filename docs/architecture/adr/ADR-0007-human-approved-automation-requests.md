# ADR-0007: Human-Approved Automation Requests Through Protected GitHub Environments

- Status: Accepted
- Date: 2026-11-09
- Deciders: svelderrainruiz (operator), who approved the recommended
  defaults for decisions D1 to D8 in `VITechnologies/projectbuddy#177`

## Context

Agent sessions, bots, and scheduled workflows propose privileged actions
for this engine: wiki publishes, issue-body edits, GitFlow sync and
promotion merges, Actions dispatches, and VM lifecycle operations. Today
an operator authorizes each one in chat. That authorization is not
recorded, nothing enforces it, and forks cannot reuse it. Several
requirements depend on a "separate operator authorization" without
defining how it is given (`REQ-060`, `REQ-061`, `REQ-072`, `REQ-083`,
`REQ-084`). `REQ-005` escalation issues notify a human; they do not
authorize an action.

The operator asked that humans approve or reject each individual
request through an environment gate in GitHub Actions, and that the
mechanism serve the organization and its forks as shared infrastructure.

GitHub platform facts, checked against GitHub Docs on 2026-11-09:

1. A job that references a protected environment waits until **one** of
   up to six required reviewers (users or teams) approves it. GitHub
   has no native quorum.
2. Prevent-self-review blocks only the user who *initiated* the run. The
   `POST .../actions/runs/{run_id}/pending_deployments` endpoint accepts
   any required reviewer's token. A requester that holds a reviewer's
   credential can therefore approve its own request.
3. Administrators can bypass protection rules unless the environment
   disallows it.
4. Referencing an environment that does not exist creates it with no
   protection rules. An unconfigured fork therefore fails open unless
   the executor verifies the approval itself.
5. GitHub releases an environment's secrets only after approval. Jobs on
   self-hosted runners are not isolated, so GitHub advises treating
   environment secrets there like repository secrets.
6. An unapproved job fails after 30 days.
7. Anyone with read access can read a run's review history
   (`GET .../actions/runs/{run_id}/approvals`).
8. On Free, Pro, and Team plans, required reviewers and environment
   secrets exist only for public repositories.
9. GitHub Docs do not state whether re-running an approved job requires
   a fresh approval.

State on 2026-11-09: neither the upstream engine nor its fork has an
environment; the organization is on the Free plan; both engine
repositories are public; and the requesting agent authenticates as the
administrator who would also review.

## Decision

Adopt GitHub Actions environments with required reviewers as the
approval authority for automation requests, specified by `SYS-REQ-028`
and `REQ-094` to `REQ-100` in the wiki, with these decisions:

- **D1, requester identity:** the requester authenticates as a GitHub
  App installation, with a token minted per session. A GitHub App can
  never be a required reviewer.
- **D2, rollout:** start with an advisory pilot that uses a no-op
  known-answer action class in the fork. Then enforce one action class,
  wiki publish.
- **D3, granularity:** one approval environment per action class. Each
  environment holds the only credential that can perform its class.
- **D4, expiry and comments:** requests expire 72 hours after
  submission. A rejection should carry a comment. GitHub cannot enforce
  that, so a rejection without one still counts as a rejection and its
  record flags the missing reason.
- **D5, durable record:** each decision is recorded as a comment on the
  adopting repository's tracking issue, so the record outlives
  run-log retention.
- **D6, private consumers on the Free plan:** their requests run in a
  public engine repository with digest-only content. Nothing private
  appears in public run summaries.
- **D7, submission:** submitting a request counts as pre-authorized,
  because the gate allows no write before approval.
- **D8, staging:** request content is staged on a `requests/*` ref that
  a ruleset restricts to the requester identity, so the requester never
  writes to a target.

Each request is one workflow run with one gated job, structured in
three stages:

1. **Preflight**, before the gate. It validates the request, checks
   target drift, runs a leak scan, applies the action-class policy, and
   publishes everything the reviewer needs in the run summary.
2. **Gate.** The job references the action class's environment and
   waits for a reviewer.
3. **Executor**, after approval and before any side effect. It confirms
   from the review history that an eligible reviewer other than the
   requester approved this run. It acts only on the first run attempt,
   within the expiry, and with the target precondition unchanged. It
   recomputes the content digest.

The executor's verification of the review history compensates for facts
3, 4, and 9: a bypass, an auto-created environment, or a re-run leaves no
write.

## Alternatives Considered

- **Pull request reviews.** A pull request review approves a code diff,
  not an action, and does not release a credential.
- **Issue-comment commands.** Any identity with issue write access can
  post them, and they do not gate secrets.
- **A custom deployment-protection-rule GitHub App.** The feature is in
  public preview, and it adds a service to operate. It remains an option
  if a quorum of more than one reviewer becomes necessary.

## Consequences

- **One queue for maintainers.** Pending requests appear in GitHub
  notifications and in `gh run list --status waiting`. A reviewer
  decides each request from its run page.
- **Approval releases a scoped credential.** Requesters stop holding
  standing write credentials.
- **Forks adopt by configuration.** A fork configures its own
  environments and reviewers. A readiness check names whatever is
  missing.
- **The gate is advisory until D1 is in place.** Until the GitHub App
  requester exists and the requester holds no reviewer credential, the
  readiness check reports the gate as advisory, not enforcing.
- **Plan dependency.** The mechanism requires public repositories on
  the organization's current plan. Private repositories are reported as
  unsupported.
- **Limits.** One approval decides a request. A request can wait no
  more than 30 days on GitHub's side, and the 72-hour expiry applies
  first.
- **Creating the GitHub App requires escalation.** Creating the App and
  configuring environments are account- and repository-administration
  actions, which go through the operator's human-escalation path.

## Related

- `VITechnologies/projectbuddy#177`: refined requirement, platform facts,
  and decisions D1 to D8.
- Wiki: `SYS-REQ-028`, `REQ-094` to `REQ-100`, and `TEST-096` to
  `TEST-102`.
- `REQ-001`: the gate's engine code names no reviewer, repository, or
  organization.
- `REQ-005`: human escalation issues, which notify a human but
  authorize nothing.
