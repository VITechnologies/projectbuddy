# Contributing

## Definition of Done

A change (PR, issue, or agent turn) is Done when all of the following are
true:

- [ ] Real CI passes (`Docs Link Check / lychee`, `PR Coverage Gate /
      coverage`), not just local validation.
- [ ] Wiki SRS/RTM/Architecture Packet
      ([VITechnologies/projectbuddy.wiki](https://github.com/VITechnologies/projectbuddy/wiki))
      are updated when behavior, a requirement, or an architectural
      decision changes -- a spec that can't accurately depict the code
      signals a real implementation problem, not just a doc gap.
- [ ] `CHANGELOG.md` has an entry for the shipped version.
- [ ] Any new gap between the spec and the implementation discovered along
      the way is filed as an issue per
      [Proactively filing issues](#proactively-filing-issues-for-spec-drift-and-future-agent-success-risks)
      below, not silently deferred.
- [ ] The next session has a filed issue and an updated `#23` (Current
      Direction) handoff before the turn ends.

## Branch model

This repo follows GitFlow (`ADR-0001`): `feature/*` branches land on
`develop`; `release/vX.Y.Z` and `hotfix/vX.Y.Z` branches land on `main`
(and merge back to `develop`). See
[`docs/release/release-procedure.md`](docs/release/release-procedure.md)
for the full flow, and
[`docs/architecture/adr/ADR-0001-adopt-gitflow-for-release-governance.md`](docs/architecture/adr/ADR-0001-adopt-gitflow-for-release-governance.md)
for why.

Every action pinned in a workflow file must be pinned to a commit SHA, not
a floating tag (`sha_pinning_required` is enabled at the repository level
to catch this automatically). Every new workflow must declare its own
explicit `permissions:` block -- the repository-wide default is `read`;
do not rely on it implicitly.

## Branch protection and the `--admin` merge pattern

Both `main` and `develop` on `VITechnologies/projectbuddy` (upstream)
require 1 approving review and the `Docs Link Check / lychee` +
`PR Coverage Gate / coverage` status checks before merging
(`enforce_admins: false`); `svelderrainruiz/projectbuddy` (the fork)
requires the same 2 checks but 0 approvals (solo maintainer -- requiring
reviews there would self-lock every merge, since GitHub does not let an
author approve their own PR).

**This means a plain `gh pr merge` on upstream for the routine
fork-to-`develop` sync PRs and `release/*`-to-`main` promotion PRs will
be rejected** ("the base branch policy prohibits the merge") since there
is no second human reviewer in that automated loop by default. Per
operator decision, this is resolved by using `gh pr merge --admin` for
*specifically this automation pattern* (sync-from-fork PRs and
release/hotfix promotion PRs, both already fully vetted by GitFlow +
required CI passing) -- not as a general bypass for arbitrary PRs. If you
are a future agent and hit this error on a sync/release PR, this is the
sanctioned resolution, not a blocker to escalate.

## Proactively filing issues for spec drift and future-agent-success risks

Whenever you (human or agent) notice any of the following, **file a
tracking issue immediately** on `VITechnologies/projectbuddy` (this
engine's central tracker) describing the concrete risk -- do not just note
it in passing, defer it silently, or assume someone else will catch it:

- A gap between what this repo's specs (the wiki SRS/RTM/Architecture
  Packet, `docs/architecture/standards-compliance.md`'s gate table, this
  `CONTRIBUTING.md`) claim and what the repo actually does.
- A configuration that would behave *differently and silently worse* on
  one repo/fork than another, in a way a future contributor might not
  anticipate -- the canonical example: a required branch-protection
  status check that depends on infrastructure (like the
  `standards-review-local` self-hosted runner) that only exists on one
  specific fork. Marking that check "required" on any repo lacking that
  runner would permanently deadlock every future PR there, with no
  obvious error message pointing at the real cause.
- An overly broad default (repo-wide Actions permissions, missing branch
  protection, an unpinned action, a missing required check) that a future
  agent could stumble into without realizing it was ever a deliberate gap
  rather than an intentional choice.

A short, concrete issue (what's wrong, why it specifically risks a future
agent/contributor, and a recommended fix) is far more valuable than a
perfect one -- file it, then iterate.
