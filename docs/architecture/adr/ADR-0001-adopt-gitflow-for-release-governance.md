# ADR-0001: Adopt GitFlow for Release Governance

- Status: Accepted
- Date: 2026-11-06
- Deciders: svelderrainruiz (operator)

## Context

- `svelderrainruiz/repo-standards-review` is now the authoritative
  engineering-maturity/release-readiness standard for this engine
  (`VITechnologies/projectbuddy`), per operator direction.
- Running its real assurance pipeline
  (`python3 scripts/run_assurance.py <checkout> --profile release-gate`)
  against this repo produced a confirmed CM gate FAIL: this repo used a
  single protected `main` branch with tags cut directly from `main` (12
  published GitHub Releases, `v0.1.0`-`v0.3.6`) -- a disciplined
  trunk-based/release-tag model, not GitFlow.
- `repo-standards-review`'s own `ADR-0001-gitflow-is-the-universal-branch-governance-doctrine.md`
  deliberately makes GitFlow the only accepted branch-governance doctrine
  for every repo it assesses, stating in its own Consequences > Negative:
  *"repos using trunk-based or GitHub-flow-like models will be scored
  non-compliant on branch governance."*
- Tracked as `VITechnologies/projectbuddy#6` (escalated for a human
  decision, since the agent assessing this could not unilaterally choose to
  restructure a repo with 12 already-published immutable releases).

## Decision

- Adopt GitFlow as this repo's branch-governance model: a long-lived
  `develop` branch alongside `main`, `feature/*` branches merging into
  `develop`, `release/vX.Y.Z` branches cut from `develop` and merged back
  into both `main` and `develop`, and `hotfix/vX.Y.Z` branches cut from
  `main` and merged back into both `main` and `develop` (or an active
  `release/*` branch, if one exists).
- **Condition attached to this decision**: every document this adoption
  produces or touches must carry a machine-checkable stamp of the exact
  `repo-standards-review` version (tag and commit SHA) that drove it, so
  the compliance claim is traceable to a specific, reproducible version of
  the standard -- not an undated, driftable assertion. In practice this
  means every such document is covered by (not a duplicate copy of) the
  one canonical stamp in
  [`../standards-compliance.md`](../standards-compliance.md), which every
  CM/release/governance doc this ADR produced
  (`docs/cm/cm-plan.md`, `docs/release/release-procedure.md`, this ADR
  itself) explicitly cross-references rather than re-stamping
  independently -- avoiding `N` separately-driftable stamps instead of
  one.
- This repo's fork, `svelderrainruiz/projectbuddy`, continues to be where
  day-to-day feature development happens; its own `main` branch now
  represents GitFlow's `develop` role. Sync pull requests from the fork
  target this repo's `develop` branch (not `main`). Release branches then
  promote `develop` to `main` here, on `VITechnologies/projectbuddy`, only.
- See [`../../release/release-procedure.md`](../../release/release-procedure.md)
  for the full, GitHub-adapted feature/release/hotfix flow.

## Rationale

- A single, explicit doctrine removes ambiguity from the standard's
  branch-governance scoring, and the operator has decided the compliance
  benefit is worth restructuring around it.
- This repo's existing SemVer/release discipline (12 published, immutable
  GitHub Releases, a maintained `CHANGELOG.md`) is preserved and extended,
  not replaced -- GitFlow adds structure around *how* a release is
  prepared and promoted, it does not change the fact that `main` remains
  the single source of truth for what is actually released.
- Stamping the standard's version addresses the practical risk of a
  compliance claim becoming meaningless as `repo-standards-review` itself
  evolves: a reader can always tell exactly which version of the standard
  this repo was assessed against, and a scheduled check (see
  `standards-compliance.md`) flags -- without silently rewriting anything
  -- when a newer version is published.

## Alternatives Considered

- **Keep the trunk-based model and document a deliberate deviation**:
  rejected by the operator in favor of adopting the standard outright.
- **Adopt GitFlow without a version stamp**: rejected, since an undated
  compliance claim degrades silently as the standard evolves, with no
  signal to anyone that a re-check is due.
- **Auto-update the stamp whenever a new standard version passes
  re-assessment**: rejected (see `standards-compliance.md`'s policy) in
  favor of always flagging a human for review, since compliance could have
  regressed and should never be silently asserted by automation.

## Consequences

- Positive:
  - This repo's CM gate has a concrete, actionable path to pass under
    `repo-standards-review`'s rubric.
  - Branch-governance expectations become explicit and documented
    (`release-procedure.md`) rather than implicit tribal knowledge.
  - The version stamp makes every future compliance claim independently
    verifiable and re-checkable, rather than a one-time, undated assertion.
- Negative:
  - Every future release now requires an extra `release/*` branch step
    (and every hotfix a `hotfix/*` branch step) that the prior trunk-based
    model did not need.
  - The fork's existing "fork `main` syncs to upstream `main`" habit
    (used throughout this engine's recent history) must change to "fork
    `main` syncs to upstream `develop`" -- a real process change for every
    future contribution, not just this one.
- Known variance: `v0.3.28` was tagged on a commit one PR past its
  `release/v0.3.28` branch's own promotion -- a second, non-release PR
  landed directly on `main` first (a real-CI-dispatch-discovered
  parse-error fix, found only by re-testing the just-merged release).
  See `docs/cm/cm-plan.md`'s Status Accounting section for the full
  record and judgment; `docs/release/release-procedure.md` step 6 now
  states the rule this deviated from explicitly, to reduce recurrence.

