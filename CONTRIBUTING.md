# Contributing

- Status: Active
- Last substantive update: 2026-11-23
- Owner: svelderrainruiz

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
(and merge back to `develop`, unless `develop` and `main` are already
content-identical, in which case the merge-back is a legitimate no-op).
See
[`docs/release/release-procedure.md`](docs/release/release-procedure.md)
for the full flow, and
[`docs/architecture/adr/ADR-0001-adopt-gitflow-for-release-governance.md`](docs/architecture/adr/ADR-0001-adopt-gitflow-for-release-governance.md)
for why.

Every action pinned in a workflow file must be pinned to a commit SHA, not
a floating tag (`sha_pinning_required` is enabled at the repository level
to catch this automatically). Every new workflow must declare its own
explicit `permissions:` block -- the repository-wide default is `read`;
do not rely on it implicitly.

## Branch protection and the develop merge queue

On upstream (`VITechnologies/projectbuddy`), every PR into `develop` goes
through the merge queue, including fork syncs and release or hotfix
merge-backs. GitHub requires **0 approving reviews** on `develop`: the
sole maintainer cannot approve their own PR. Independent review is still
required before queueing:

1. Have an independent reviewer review the **exact PR head**. Record the
   head SHA and the review result on the PR. Resolve all High and Medium
   findings; if the head changes, review and record the new head.
2. Wait for `Docs Link Check / lychee` and `PR Coverage Gate / coverage`
   to pass on the PR. Use `gh pr merge <n> --merge` to add it to the
   queue. **Never use `--admin` on `develop`**: it would bypass the queue.
3. Both checks run again on the queue's merge-group commit. The queue
   merges only after both pass on that commit.

The [develop ruleset](.github/rulesets/develop-merge-queue.json) defines
the queue; the two checks and approval count are branch-protection settings.
Committing the JSON does not apply it. [#421](https://github.com/VITechnologies/projectbuddy/issues/421)
tracks activation and the first real queued merge.

**Upstream `main` is unchanged** pending the follow-up in #421: it still
requires 1 approving review and the same two checks (`enforce_admins: false`).
Only for an already-vetted release or hotfix promotion into `main`, when
both checks have passed and the solo-maintainer approval is the only
blocker, the existing `gh pr merge --admin` exception still applies.
The fork (`svelderrainruiz/projectbuddy`) keeps its two required checks
and 0 required approvals; this queue does not apply there.

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
