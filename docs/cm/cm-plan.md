# Configuration Management Plan

- Status: Active
- Last substantive update: 2026-11-07
- Owner: svelderrainruiz

Satisfies `repo-standards-review`'s CM gate (`VITechnologies/projectbuddy#6`):
baseline rule, change control, and status accounting evidence, alongside
the GitFlow branch governance already adopted in `ADR-0001`.

## Scope

- Product or service: `VITechnologies/projectbuddy` (the public engine) and
  its `svelderrainruiz/projectbuddy` development fork.
- Managed baselines: every tagged `vX.Y.Z` commit on `main`, each backed by
  an immutable GitHub Release (assets and tag cannot change after
  publishing, per this repo's release-immutability policy).

## Configuration Items

| CI | Type | Owner | Baseline Rule |
| --- | --- | --- | --- |
| Source repo (`main`) | Code | Operator | Tagged release only, via `release/*` -> `main` |
| `develop` | Code | Operator | Integration branch; never released from directly |
| Release notes | Document | Operator | One per tag, generated from `CHANGELOG.md`'s matching entry (historical exceptions: `v0.2.2`/`v0.3.0` are tagged with no matching `CHANGELOG.md` entry; `v0.3.7`/`v0.3.8`/`v0.3.11`/`v0.3.12` have `CHANGELOG.md` entries but were never tagged/released -- all predate this CM plan's introduction) |
| `docs/architecture/standards-compliance.md` | Document | Operator | Versioned with repo; stamp updated only after human review |
| Wiki (SRS/RTM/Architecture Packet) | Document | Operator | Versioned via the wiki's own git history (direct pushes to `master` in practice -- confirmed zero branches used to date; see [the wiki Home page](https://github.com/VITechnologies/projectbuddy/wiki)); tagged `vX.Y.Z` to match the main-repo release it was current for, as of `v0.3.31` onward (earlier releases' wiki states are identifiable only by commit timestamp, not a tag) |

## Versioning

- Scheme: `vX.Y.Z` (SemVer), tagged directly on `main`.
- Tag trigger: manual, after a `release/vX.Y.Z` branch (see below) has been
  promoted to `main` and its merge-back to `develop` confirmed, or
  confirmed as a legitimate no-op if `develop` and `main` were already
  content-identical (`docs/release/release-procedure.md`).
- Release branch rule: `release/vX.Y.Z` branches are cut from `develop`,
  merged to `main` first, then merged back into `develop`, then deleted.

## Change Control

| Change Type | Approval | Timing |
| --- | --- | --- |
| Standard (new feature/fix) | Operator review of the PR before merge to `develop` -- on the solo-maintainer fork this is 0 required approvals (no second reviewer exists to approve); on upstream, branch protection requires 1 approval and passing CI, enforced via `gh pr merge --admin` for the routine, already-fully-vetted fork-sync and release/hotfix promotion PRs specifically (see `CONTRIBUTING.md`'s "Branch protection and the `--admin` merge pattern"), never as a general bypass | Any time, via `feature/*` branches (in practice, often a more specific topic-branch name serving the same role, e.g. `round6-semantic-audit-fixes`) |
| Urgent (hotfix) | Same approval mechanism as Standard, applied to `hotfix/vX.Y.Z` -> `main` | As needed, via `hotfix/vX.Y.Z` branches cut from `main` |
| Concession (known, accepted gap) | Operator decision, recorded as a comment or ADR | Documented at the time the gap is identified, not silently carried forward |

## Status Accounting

- Record location: `CHANGELOG.md` (one entry per released version,
  describing what changed and why) and each version's GitHub Release notes
  (identical content, published alongside the immutable tag).
- Release record owner: the operator (svelderrainruiz), as the sole
  maintainer authorizing each release/hotfix promotion.
- Audit trail: every release since GitFlow adoption (`ADR-0001`,
  `v0.3.9` onward) has its exact promoted commit independently
  verifiable via `git log --oneline` on `main` at that tag, cross-referenced
  against the PR history for the `release/*`/`hotfix/*` branch that
  produced it (GitHub's PR merge history is retained even after the source
  branch is deleted). Earlier releases (pre-`v0.3.9`) were tagged directly
  under the prior trunk-based/release-tag model (`ADR-0001`) and do not
  have this same `release/*` branch ancestry. **Known exception:**
  `v0.3.28` was tagged on a commit (`cc3b384`) one PR past its
  `release/v0.3.28` branch's own promotion (`#72`) -- a second,
  non-release PR (`#73`, a real-CI-dispatch-discovered parse-error fix
  found only by actually re-testing the just-merged release) landed
  directly on `main` before tagging, since the tag had not yet been cut
  and treating it as "still the same release" was judged lower-risk than
  either leaving a known-broken `release.yml` tagged, or cutting a
  second release branch for one two-line fix. Recorded here as a
  deliberate, judged exception, not a silent deviation. The `hotfix/*`
  flow this exception most resembles has never actually been exercised
  in this repo's real history (zero `hotfix/*` branches or PRs, ever) --
  the Change Control table's "Urgent (hotfix)" row describes a mechanism
  with no real example of its own use yet.
- Configuration audits (ISO/IEC/IEEE 12207:2017 S6.3.5 outcome (e)): the
  semantic-audit pipeline (`svelderrainruiz/repo-standards-review`'s
  `scripts/semantic_audit.py`, see
  [`../architecture/standards-compliance.md`](../architecture/standards-compliance.md))
  is this repo's real, checkable mechanism for periodic configuration
  audits -- every semantic-audit round (5 to date) is a real audit of
  this repo's spec content against real standard clause text and real
  source, with findings tracked as GitHub issues and fixes shipped as
  tagged releases.
