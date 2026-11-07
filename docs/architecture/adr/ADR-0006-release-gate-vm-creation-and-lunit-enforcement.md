# ADR-0006: Release Gate Requires Live VM-Creation and Runner-Registration Proof for Both Platforms

- Status: Accepted
- Date: 2026-11-07
- Deciders: svelderrainruiz (operator), informed by 3 independent
  pre-implementation consensus reviews (background agents) that found
  convergent, real strategic defects in this ADR's first draft -- this
  is the revised decision, not the original

## Context

The operator's stated release-readiness expectation: *"in order to
release, the VM for ubuntu and windows need to be able to be created
successfully."* Scoping this found two independent gaps:

1. **`release.yml` currently verifies nothing automatically.** It is a
   pure tag/changelog/publish mechanism with zero reference to the
   LUnit gate or VM provisioning. Even `REQ-033` -- *"a passing JUnit
   report from both the Windows and Linux ephemeral LabVIEW-container
   gates... before that release ships"* -- is currently an
   **aspirational, unenforced** requirement: nothing in
   `docs/release/release-procedure.md`'s Release Flow checks it before
   tagging.
2. **Ubuntu had no runner-registration automation at all** before this
   session (`REQ-038`, `#105`/`#106`); it has since been built and
   **live-verified** (upstream run `42403330593`: a genuine,
   freshly-built disposable VM registered a real GitHub Actions runner,
   independently confirmed `status: "online"` with the expected labels
   via `gh api`, not merely the registration command's own exit code).
   Three real bugs were found and fixed only by actually dispatching it
   live (missing `SSH_ASKPASS`; `gh api --jq` does not accept jq's own
   `--arg` flag; a re-registration ordering bug) -- underscoring why
   this ADR requires the same for Windows before relying on it (next
   point).

### First-draft review findings (3 independent agents, all converged)

Before implementing, this ADR's first draft was reviewed independently
by 3 agents (matching this project's standing pre-implementation
consensus-review practice for a new release-gating ADR). All three,
without seeing each other's output, converged on the same core defects:

- **`register-runner` is not VM-creation proof.** Both platforms'
  `register-runner` phases operate on an *already-existing, running*
  VM (Windows's own `create-vm`, Ubuntu's own `provision-public`, are
  the actual creation steps). A manually-created or stale VM could pass
  a `register-runner`-only check even if fresh VM creation is broken.
  **Operator decision in response: require BOTH checks per platform**
  (VM-creation phase success + `register-runner` phase success), not
  one check mischaracterized as covering both.
- **Windows `register-runner` had never actually been live-dispatch-proven.**
  No `RUNNER_REGISTRATION_PAT` secret existed anywhere (`gh secret list`
  against both `VITechnologies/projectbuddy` and
  `svelderrainruiz/projectbuddy` showed neither configured it) --
  treating its historical "success" as a trustworthy release-gate
  baseline was itself an unverified claim. **Operator decision: prove
  it live before finishing this ADR's implementation** (tracked
  separately, in progress at authoring time -- a disposable Windows VM
  build, `#110`).
- **Exact-SHA binding gap.** `release.yml`'s `inputs.target` accepts a
  mutable branch name or a SHA; nothing resolves it once. Without a fix,
  the gate could verify commit A while `main` advances and
  `gh release create --target` tags commit B.
- **No cleanup/teardown contract** for the disposable runners this gate
  repeatedly registers.
- **Weaker LUnit re-validation than originally described** -- the first
  draft's checks covered `outcome`/`engineCommit`/waivers but not
  `suiteCommit`/`totalTests`/`vipmPresence`, despite claiming to
  "independently re-validate" the result.
- **Missing `actions: read` permission** for the new `gh run
  list`/`gh api` calls `release.yml` would need.
- **No freshness handling.** One reviewer correctly noted a *literal*
  "different commit state of the same SHA" is impossible (a SHA fully
  determines its tree) -- but real staleness still exists: `gate-result.json`
  artifacts are retained only 90 days
  (`lunit-gate-{linux,windows}.yml`'s own `retention-days: 90`), and
  external dependencies (the pinned `suite_commit`, credentials) can
  change even though the release commit's own SHA cannot.

## Decision

`release.yml` gains two new jobs, both required (`needs:`) before
`Create Release` runs:

### 1. `resolve-release-sha`

Resolves `inputs.target` (a branch name or a SHA) to a full, immutable
40-character commit SHA exactly once, via `git rev-parse`, and exposes
it as a job output (`release_sha`). Every subsequent job -- the new
precondition-verification job, the changelog-extraction step, and
`gh release create --target` itself -- consumes this **same** resolved
SHA, never the original mutable `inputs.target` string. This closes the
exact-SHA binding gap: `main` advancing after resolution can no longer
cause the gate to verify one commit while tagging another.

### 2. `verify-release-preconditions`

Runs `.github/scripts/verify-release-preconditions.py <release_sha>`
(new script), with `permissions: { actions: read, contents: read }`
explicitly granted to this job (narrower than the release-creation
job's `contents: write`). Fails closed -- non-zero exit, a clear
message naming exactly which check failed and why -- unless, for that
exact resolved SHA, **all six** of the following hold:

| # | Check | Evidence source |
| --- | --- | --- |
| 1 | `lunit-gate-windows.yml`: a `workflow_dispatch`, `conclusion: success` run at this SHA | `gh run list -w lunit-gate-windows.yml -c <sha> -e workflow_dispatch -s success`, then `gh run download <run_id> -n lunit-gate-windows-results-<run_id>` (the artifact name already embeds the run ID -- no name-guessing needed) |
| 2 | That run's `gate-result.json`: `outcome == "PASS"`, `engineCommit == <sha>`, `suiteCommit` matches the one authoritative pinned value this script hardcodes (not merely "any valid 40-char SHA"), `totalTests` matches the expected pinned-suite count, `vipmPresence == false`, `waivedTests` is a subset of the single Windows-authorized waiver (`ADR-0003`) | Same artifact |
| 3 | `lunit-gate-linux.yml`: same as #1, for Linux | Same mechanism |
| 4 | That run's `gate-result.json`: same as #2, with `waivedTests` checked against the three Linux-authorized waivers (`ADR-0003`/`ADR-0004`) | Same artifact |
| 5 | `provision-windows-labview-vm.yml`: a `workflow_dispatch`, `conclusion: success` run at this SHA whose `displayTitle` shows `phase=create-vm` **and** a separate (not necessarily the same run) `conclusion: success` run at this SHA whose `displayTitle` shows `phase=register-runner` | `gh run list -w provision-windows-labview-vm.yml -c <sha> -e workflow_dispatch -s success --json displayTitle,...`; phase disambiguation via the new `run-name:` directive below |
| 6 | `provision-ubuntu-vm.yml`: same as #5, with `phase=provision-public` and `phase=register-runner` | Same mechanism |

**New `run-name:` convention** (both `provision-windows-labview-vm.yml`
and `provision-ubuntu-vm.yml`), the only way to recover a past
dispatch's phase since GitHub's API does not expose `workflow_dispatch`
input values after a run completes:

```yaml
run-name: "${{ github.workflow }} (phase=${{ inputs.phase }}, vm=${{ inputs.vm_name }})"
```

**Explicitly NOT re-implemented at the release.yml layer**: a second,
live `gh api .../actions/runners` cross-check of checks 5/6's
`register-runner` runs. `REQ-020`/`REQ-038` already perform that
cross-check *inside* their own phase, at dispatch time; `release.yml`
trusts that a `register-runner` run's own `conclusion: success` already
reflects it. Re-querying a (by now likely deregistered, since a
disposable qualification runner is not meant to stay registered
indefinitely) runner's live status a second time at release time would
conflate "was this capability proven to work" with "is that specific
ephemeral runner still online right now" -- not what this gate checks.

**Mandatory cleanup/teardown contract** for any qualification dispatch
of `register-runner` performed specifically to satisfy checks 5/6:
deregister the runner (`gh api --method DELETE
repos/<target>/actions/runners/<id>`) and power off/destroy the
disposable VM afterward, on both success and failure -- this is an
operational runbook requirement for whoever dispatches a qualification
run, not new code (matches how this session's own live-verification
of `REQ-038` was cleaned up: runner deregistered and the temporary PAT
secret removed immediately after).

**Freshness**: `verify-release-preconditions.py` additionally rejects a
qualifying LUnit run (checks 1-4) older than 90 days (matching
`gate-result.json`'s own artifact retention window) even if the SHA
still matches, since the artifact itself would no longer be
downloadable by then regardless. No equivalent age limit is imposed on
checks 5/6 (provisioning-workflow runs), since those checks rely only
on run metadata (`headSha`/`conclusion`/`displayTitle`), which GitHub
retains independently of artifact retention.

**Scope boundary** (unchanged from the first draft): this ADR only
fixes `release.yml`'s own precondition verification. It does not
retroactively gate any already-published release.

**Operational cost, explicitly owned, not understated**: no commit in
this repository's history currently has all 6 qualifying runs. The
first real release attempted after this ADR ships will require a
fresh, deliberate re-dispatch of all 4 provisioning-workflow checks
(create-vm/provision-public + register-runner, both platforms) and
both LUnit gates against the actual release-candidate commit, before
that release can be tagged. `docs/release/release-procedure.md`'s
Release Flow must gain an explicit new step describing this, as part
of this ADR's implementation (not left as a documentation gap the way
`REQ-033` was left before this ADR).

## Consequences

- `release.yml` becomes slower and more complex, but closes two real,
  previously-silent gaps: `REQ-033`'s LUnit requirement was never
  actually enforced, and nothing ever checked VM-creation or
  runner-registration success at all.
- A release candidate commit with any of the 6 checks missing cannot
  ship -- this is the entire point, but requires the release-procedure
  update above so this isn't a silent trap for the next release attempt.
- New wiki `REQ-039` (Planned) records this; promotes to Active only
  once genuinely live-verified: one real `release.yml` dispatch where
  the gate demonstrably fails closed (a commit missing at least one of
  the six), and one where it demonstrably passes (a commit with all six
  present).
- Windows `register-runner`'s own live-proof (in progress, `#110`) is a
  hard prerequisite for this ADR's implementation to be trustworthy,
  not just for completeness -- a release gate that leans on an
  unproven capability as if it were proven would be worse than no gate
  at all.

## Related

- `REQ-020`, `REQ-033`, `REQ-034`, `REQ-036`, `REQ-038`
- `ADR-0003`, `ADR-0004` (the waiver-authorization rules this ADR's
  gate cross-checks against, left unedited)
- `#105`/`#106`/`#107`/`#108`/`#109` (`REQ-038`'s Ubuntu `register-runner`
  parity + live verification + the 3 real bugs found while proving it,
  all precedent for why Windows needs the same live-proof discipline
  before this ADR relies on it)
- `#110` (Windows `register-runner` live-proof, tracked separately)
- Wiki `REQ-039`
