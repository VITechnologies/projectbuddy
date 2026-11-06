# Configuration Management Plan

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
| Release notes | Document | Operator | One per tag, generated from `CHANGELOG.md`'s matching entry |
| `docs/architecture/standards-compliance.md` | Document | Operator | Versioned with repo; stamp updated only after human review |
| Wiki (SRS/RTM/Architecture Packet) | Document | Operator | Versioned via the wiki's own git history (`branch-then-push`, see [[Home]] on the wiki) |

## Versioning

- Scheme: `vX.Y.Z` (SemVer), tagged directly on `main`.
- Tag trigger: manual, after a `release/vX.Y.Z` branch (see below) has been
  promoted to `main` and its merge-back to `develop` confirmed (`docs/release/release-procedure.md`).
- Release branch rule: `release/vX.Y.Z` branches are cut from `develop`,
  merged to `main` first, then merged back into `develop`, then deleted.

## Change Control

| Change Type | Approval | Timing |
| --- | --- | --- |
| Standard (new feature/fix) | Operator review of the PR before merge to `develop` | Any time, via `feature/*` branches |
| Urgent (hotfix) | Operator review of the PR before merge to `main` | As needed, via `hotfix/vX.Y.Z` branches cut from `main` |
| Concession (known, accepted gap) | Operator decision, recorded as a comment or ADR | Documented at the time the gap is identified, not silently carried forward |

## Status Accounting

- Record location: `CHANGELOG.md` (one entry per released version,
  describing what changed and why) and each version's GitHub Release notes
  (identical content, published alongside the immutable tag).
- Release record owner: the operator (svelderrainruiz), as the sole
  maintainer authorizing each release/hotfix promotion.
- Audit trail: every release's exact promoted commit is independently
  verifiable via `git log --oneline` on `main` at that tag, cross-referenced
  against the PR history for the `release/*`/`hotfix/*` branch that
  produced it (GitHub's PR merge history is retained even after the source
  branch is deleted).
