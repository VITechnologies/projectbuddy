# Human Approval Gate Procedure (ADR-0007)

- Status: Phase 1. Built and fixture-tested offline; not yet live-proven.
  The pilot stays advisory until the GitHub App requester (D1) and the
  `approval-noop` environment exist (`VITechnologies/projectbuddy#179`).
- Last substantive update: 2026-11-09
- Owner: svelderrainruiz

Implements
[ADR-0007](../architecture/adr/ADR-0007-human-approved-automation-requests.md)
and the wiki's `SYS-REQ-028` and `REQ-094` to `REQ-100`. An automated
requester, such as an agent, a bot, or a scheduled workflow, asks for one
privileged action. A human approves or rejects it on GitHub, and the action
runs only after the gate has verified that approval.

The pieces:

- `scripts/Invoke-ApprovalGate.py`: the command line for requesters,
  reviewers, and the gate's own jobs. It needs only Python 3.11 or later,
  plus `openssl` when it signs in as a GitHub App.
- `.github/workflows/approval-request.yml`: the dispatch entry point. Each
  request is one run of it.
- `.github/workflows/approval-gate.yml`: the reusable gate. It holds the
  `Preflight`, `Execute approved request`, and `Record` jobs.
- `.github/approval-gate/classes.json`, in the adopting repository: the
  closed list of actions a request may ask for. See
  [classes.example.json](classes.example.json).

## Roles

| Role | Who | Does |
| --- | --- | --- |
| Requester | A GitHub App installation (D1). Before the App exists, any token that is not a reviewer. | Submits requests with `submit`. Never approves. |
| Reviewer | A human required reviewer of the action class's environment. | Approves or rejects the waiting `Execute approved request` job. |
| Maintainer | Whoever administers the gate repository. | Owns the action classes, environments, reviewers, and rulesets. |
| Gate | `approval-gate.yml` | Publishes the request, waits for the decision, re-verifies it, runs the executor, and records the outcome. |

## What the gate guarantees

- **No write before approval.** A class's credential exists only as a
  secret of that class's environment. GitHub releases it only to the gated
  job, and only after a reviewer approves.
- **Approval is re-verified before any side effect.** The gated job reads
  the review history itself and refuses unless all of the following hold:
  - the run is the first attempt of a `workflow_dispatch` run on the
    default branch, triggered by the requester the request names;
  - the environment is still protected;
  - a reviewer other than the requester approved it;
  - the request has not expired;
  - the target and content are unchanged.
  See [Refusal reasons](#refusal-reasons).
- **Content cannot change after submission.** The request carries a
  SHA-256 digest of its canonical JSON. Staged content goes on an orphan
  `requests/<id>` ref, which is never overwritten. A request ID can be used
  only once.
- **Every decision is recorded.** The `Record` job always runs, even after
  a rejection or a cancellation. It comments on the request's tracking
  issue as `github-actions[bot]`, and the tool trusts only records by that
  author.
- **Public summaries stay clean.** Free text, every identifying field
  (including a staged file's ref and path), and every staged file are
  leak-scanned, and a finding blocks the request. Files are scanned in
  every plausible encoding, so UTF-16 output from Windows PowerShell 5.1
  is covered too. The gate refuses staged content that it cannot scan
  whole: more than 50 files, more than 8 MiB, or a submodule. After a
  finding, the run summary withholds every request-derived detail. The
  decision record never includes free text, and it withholds the
  request's identifying fields whenever any of them matches.
- **Records cannot be forged through request text.** Request IDs, class
  names, and requester logins have strict formats, and the tool reads a
  record only from the final line of a comment by the record author.

## What it does not guarantee

- **Enforcement needs more than an environment.** The gate is advisory
  while the requester can write the gate's default branch, because that
  requester could change the gate itself. `readiness` reports this. See
  [Checking readiness](#6-check-readiness).
- **One approval decides a request.** GitHub has no reviewer quorum.
- **The approval time is approximate.** GitHub's approvals API carries no
  timestamp, so the record uses the time the gated job started.
- **Branch policy patterns are not inspected.** `readiness` checks only
  that the environment has a deployment branch policy. Confirm by hand that
  the policy admits only the default branch.
- **A rejection may carry no reason.** Reviewers should comment when they
  reject (D4). GitHub cannot require a comment, so the record flags a
  missing one.
- **Self-hosted runners do not isolate secrets.** Any process on the
  machine can read an environment secret released to a job there. The
  gate's jobs run on GitHub-hosted runners. An executor that needs a
  self-hosted runner must have a dedicated, ephemeral one.
- **Private repositories depend on the plan.** Where the plan offers no
  required reviewers for private repositories, `readiness` reports
  `unsupported`. Private consumers submit to a public gate repository with
  digest-only content instead (D6).

## Setting up the gate

Run these steps in the repository that hosts the gate. Steps 3 to 5 need
repository administration, and step 4 needs account administration.
Request those through the human-escalation path; the requester never
holds that access.

### 1. Add the request workflow

ProjectBuddy and its forks ship `approval-request.yml` already. Any other
repository copies that file and pins the reusable gate to a commit:

```yaml
    uses: OWNER/projectbuddy/.github/workflows/approval-gate.yml@COMMIT_SHA
    with:
      engine-repository: OWNER/projectbuddy
      engine-ref: COMMIT_SHA
```

Keep these parts of the copied file unchanged:

- **The `run-name` line.** The tool finds requests by parsing it.
- **The explicit `secrets:` block.** Do not replace it with
  `secrets: inherit`.
- **The job permissions** (`contents: read`, `actions: read`,
  `issues: write`).

### 2. Declare the action classes

Commit `.github/approval-gate/classes.json` to the default branch. The
gate reads the file at the run's commit, and `submit` reads it from the
default branch. Neither ever reads it from request inputs. Without the
file, only the built-in `noop` class exists, and it matches
[classes.example.json](classes.example.json). The registry refuses unknown
keys, so a misspelled key cannot fall back silently to a looser default.

| Key | Required | Meaning |
| --- | --- | --- |
| `executor` | yes | The built-in executor to run. Only `noop` exists so far. |
| `environment` | yes | The approval environment. Each class needs its own; two classes may not share one. |
| `targets` | yes | Repositories the class may act on. `@self` means the gate repository. Glob patterns are accepted, but they keep the class advisory. |
| `refs` | yes | `@default`, meaning the gate's default branch, or patterns under `refs/heads/`. |
| `content_kinds` | no | `commit`, `sha256`, or both (the default). |
| `precondition_kinds` | no | `ref-sha`, `issue-edited`, or both (the default). |
| `default_expiry_hours` | no | The expiry used when the requester gives none. Defaults to 72, capped at `max_expiry_hours`. |
| `max_expiry_hours` | no | 1 to 720. Defaults to 72. GitHub fails a review wait after 30 days. |
| `allow_digest_only` | no | Defaults to `false`, which requires `sha256` content staged in the gate repository so reviewers can read it. `true` lets the payload stay with the requester (D6). |
| `description` | no | Shown in the run summary. |

### 3. Create one environment per class

In the repository settings, open **Environments** and create one named
after the class's `environment`, with these settings:

- **Required reviewers:** humans, as users or teams, who never act as the
  requester. Listing users is safer than listing teams: when team
  membership is unreadable, `submit` refuses to go ahead.
- **Prevent self-review:** on.
- **Allow administrators to bypass configured protection rules:** off.
- **Deployment branches and tags:** selected branches, and only the
  default branch.
- **Environment secrets:** the class's credential, which lives nowhere
  else. For the `noop` pilot, add `APPROVAL_GATE_PROBE` with any non-empty
  value instead.

Never define `APPROVAL_GATE_PROBE` as a repository or organization
secret. Preflight runs outside the environment, and its
`secret-isolation` check fails if it can see the probe. Inside the gated
job the environment's value applies, and the `noop` executor reports only
whether the probe was visible. Together these show that the environment
released its secret only after approval.

### 4. Create the requester GitHub App (D1)

Create a GitHub App and install it on the gate repository only. A GitHub
App can never be a required reviewer, which is why D1 makes it the
requester.

The expected minimum permissions are below. Phase 2 of
`VITechnologies/projectbuddy#179` confirms them live, and `readiness`
names any read it cannot make.

- Actions: read and write, to dispatch, follow, and withdraw runs.
- Contents: read and write, to stage `requests/<id>` refs.
- Issues: read, to read decision records.
- Metadata: read.

The requester runs the tool with `APPROVAL_GATE_APP_ID` and
`APPROVAL_GATE_APP_KEY_FILE`, the path to the App's private key. The tool
signs a short-lived JWT locally with `openssl` and mints an installation
token scoped to the gate repository. The key itself never leaves the
machine.

Without an App, the tool uses the first token it finds in
`APPROVAL_GATE_TOKEN`, `GH_TOKEN`, or `GITHUB_TOKEN`, and then falls back
to `gh auth token`. A user token works for trials, but at best the class
stays advisory, and the tool refuses outright if that user is a reviewer.

### 5. Add rulesets and a tracking issue

- **Staging refs (D8).** Add a ruleset on `refs/heads/requests/*` that
  blocks creation, update, and deletion for everyone except the requester
  App. Then only the requester stages content, and no one rewrites it.
- **Default branch.** Add a ruleset on the default branch that the
  requester cannot bypass. This clears `gate-writable`.
- **Targets.** For each concrete target, either remove the requester's
  write access or protect the ref with a ruleset the requester cannot
  bypass. This clears `requester-can-write`.
- **Tracking issue.** Open an issue to hold decision records. Each request
  names it with `--tracking-issue`. The `Record` job writes one comment
  per run attempt.

### 6. Check readiness

Run this as the requester identity:

```sh
python scripts/Invoke-ApprovalGate.py readiness --repo OWNER/NAME
```

| Status | Exit | Meaning |
| --- | --- | --- |
| `enforcing` | 0 | Every class is protected, and the requester cannot write the gate or the targets. |
| `advisory` | 21 | Approvals work, but the requester could act around them. |
| `not-ready` | 22 | A required setting is missing. Requests for that class would be refused. |
| `unsupported` | 23 | The repository's plan offers no required reviewers. |

Each finding carries a code and its fix:

| Code | Blocks | Fix |
| --- | --- | --- |
| `environment-missing` | yes | Create the environment as in step 3. |
| `environment-unreadable` | yes | Use a token that can read the repository's environments. |
| `required-reviewers-missing` | yes | Add at least one required reviewer who is not the requester. |
| `self-review-allowed` | yes | Turn on **Prevent self-review**. |
| `admin-bypass-allowed` | yes | Turn off administrator bypass. |
| `branch-policy-missing` | yes | Limit deployment branches to the default branch. |
| `requester-unknown` | yes | Authenticate as the requester, or set `APPROVAL_GATE_REQUESTER`. |
| `requester-is-reviewer` | yes | Submit as an identity that is not a reviewer, such as the App. |
| `reviewer-membership-unknown` | yes | Let the requester read organization members, or list users instead of teams. |
| `requester-is-admin` | yes | Remove the requester's administrator access. |
| `plan-unknown` | yes | Use a token that can read the organization's plan. |
| `plan-unsupported` | yes | Make the repository public, or change plans. |
| `gate-writable` | no | Protect the default branch with a ruleset the requester cannot bypass. |
| `requester-can-write` | no | Remove the requester's write access to the target, or protect the target ref. |
| `targets-not-enumerable` | no | List concrete repositories and refs instead of patterns. |

## Submitting a request

Request IDs are 3 to 63 characters: lowercase letters, digits, `.`, `_`,
and `-`, starting with a letter or digit. An ID is single-use. To change
anything after submitting, withdraw the request and submit again under a
new ID.

```sh
python scripts/Invoke-ApprovalGate.py classes --repo OWNER/NAME
python scripts/Invoke-ApprovalGate.py submit --repo OWNER/NAME \
  --id pilot-001 --class noop --digest SHA256_HEX \
  --rationale @rationale.md --expected-effect "Nothing; known-answer pilot." \
  --rollback "None needed." --tracking-issue 12 --dry-run
```

Drop `--dry-run` to submit for real, and add `--wait` to wait for the
decision. Free-text options accept `@file`, which keeps long text out of
shell history. `--request FILE` submits a complete request JSON
(`approval-gate/request@1`) instead of building one from options, and its
`requester` must match the authenticated identity.

Bind the content with exactly one of these:

- `--commit SHA`: a commit that already exists in the target repository.
- `--stage PATH...`: files staged as an orphan commit on
  `requests/<id>`. Paths must be plain relative paths, and they may not
  contain workflow files or `.git` components.
- `--stage-file PATH`: one staged file, bound by its SHA-256.
- `--digest SHA256`: a payload that stays with the requester. Only
  classes with `allow_digest_only` accept it.

The precondition defaults to the target ref's current SHA. Use
`--precondition-sha` to name the expected SHA explicitly, or
`--precondition-issue` to bind to an issue body's last edit instead. If
the target moves after submission, the gate refuses the request with
`drift`.

`submit` runs these steps in order and stops at the first refusal:

1. Authenticate, then read the classes from the default branch.
2. Build the request and run the offline checks: schema, class policy,
   expiry, staged paths, and the leak scan.
3. Confirm that the class's environment is protected and that the
   requester is not an eligible reviewer.
4. With `--require-enforcing`, refuse unless `readiness` reports
   `enforcing`.
5. Confirm that the target has not already drifted and that the request
   ID is unused. `--dry-run` stops here.
6. Stage the content and check it.
7. Dispatch the request workflow.
8. Once the run waits at the gate, confirm that this identity cannot
   approve it, and withdraw the run if it can.

Follow a request with `status <id>`. `withdraw <id>` cancels a request
that is still pending. `sweep` cancels waiting requests that have passed
their expiry; add `--dry-run` to list them only.

## Reviewing a request

1. Find waiting requests through GitHub notifications, `gh run list
   --status waiting`, or this command, which sorts by soonest expiry and
   accepts `--repo` more than once:

   ```sh
   python scripts/Invoke-ApprovalGate.py queue --repo OWNER/NAME
   ```

2. Open the run and read the `Preflight` summary: the digest, class,
   environment, target, content and its **Inspect** link, precondition,
   requester, expiry, rationale, expected effect, rollback, and every
   check.
3. Approve only when every check passed, the content behind **Inspect** is
   what you expect, and you are not the requester.
4. Otherwise, reject with a comment saying why. Reject anything that shows
   `withheld`.

An approval does not act by itself: the gated job re-verifies it first.
If the target moved after you approved, the outcome is `refused:drift`,
and the requester must submit a new request.

## What a run does

1. **`Preflight`** runs outside any environment, with read-only access.
   It validates the request, applies the class policy, checks the
   precondition and the content, confirms that the environment is
   protected, runs the `secret-isolation`, `staged-scannable`, and
   leak-scan checks, and publishes the summary. If any check fails, the
   gated job never starts.
2. **`Execute approved request`** waits in the class's environment until
   a reviewer acts. After an approval, it re-verifies everything below and
   only then runs the executor, exactly once.
3. **`Record`** always runs. It writes the decision and outcome to the
   tracking issue as a readable comment carrying a machine-readable
   payload.

### Refusal reasons

The gated job checks these in order and stops at the first that applies:

| Reason | Meaning |
| --- | --- |
| `environment-mismatch` | The job ran in an environment other than the class's. |
| `re-run` | This is not the run's first attempt. An approval releases only the first. |
| `run-unreadable` | The run's own metadata could not be read. |
| `untrusted-ref` | The run was not a `workflow_dispatch` on the default branch. |
| `requester-mismatch` | The run's triggering actor is not the requester the request names. |
| `environment-unprotected` | The environment lost its protection after submission. |
| `history-unreadable` | The review history could not be read. |
| `rejected` | A reviewer rejected the request. |
| `self-approved` | The requester approved its own request. |
| `no-approval` | The review history holds no approval for this environment. |
| `expired` | The approval came after the expiry. |
| `drift` | The target or issue precondition changed. |
| `digest-mismatch` | The staged content no longer matches its binding. |

### Decisions and exit codes

`status`, `submit`, and `submit --wait` exit with the request's state:

| Decision | Outcome | Exit |
| --- | --- | --- |
| approved | `executed` | 0 |
| rejected | `refused:rejected` or `not-run` | 10 |
| expired | `refused:expired` or `not-run:expired` | 11 |
| approved | `refused:drift` or `refused:digest-mismatch` | 12 |
| failed | any other `refused:<reason>`, `not-run:<reason>`, `failed:executor-error`, or `record-missing` | 12 |
| pending | the run is queued or waiting | 20 |

Other exits: 1 means an error, such as a GitHub API failure. 2 means
invalid input. 3 means the tool refused before or after dispatch.

## Secrets

- `APPROVAL_GATE_PRIVATE_PATTERNS`: an optional repository or organization
  secret holding extra leak-scan patterns, one regular expression per
  line, such as internal host names. The patterns themselves never appear
  in output.
- `APPROVAL_GATE_PROBE`: an environment secret only, as described in
  step 3.

## Related

- [ADR-0007](../architecture/adr/ADR-0007-human-approved-automation-requests.md):
  the decision, D1 to D8, and the platform facts behind it.
- Wiki `SYS-REQ-028`, `REQ-094` to `REQ-100`, and `TEST-096` to
  `TEST-102`: the requirements and their tests.
- `VITechnologies/projectbuddy#179`: the delivery phases. Phase 2 creates
  the App and the pilot environment, and Phase 3 is the live proof.
