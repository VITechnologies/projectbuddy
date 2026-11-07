# Release Procedure

GitHub-adapted equivalent of `svelderrainruiz/repo-standards-review`'s own
`docs/release/release-procedure.md`, using `gh`/GitHub Actions in place of
its GitLab-specific `scripts/pipeline.py` merge-request automation. See
[`../architecture/adr/ADR-0001-adopt-gitflow-for-release-governance.md`](../architecture/adr/ADR-0001-adopt-gitflow-for-release-governance.md)
for why this repo adopted GitFlow, and
[`../architecture/standards-compliance.md`](../architecture/standards-compliance.md)
for the one canonical `repo-standards-review` version stamp this document
is covered by (not independently re-stamped).

## GitFlow Branch Rules (summary)

- Feature branches originate from `develop`.
- Feature branches merge into `develop`.
- Release branches originate from `develop`.
- Release branches merge into `main`.
- Release branches also merge into `develop`.
- Delete the release branch after both required merges complete.
- Hotfix branches originate from `main`.
- Hotfix branches merge into `main`.
- Hotfix branches also merge into `develop`, and additionally into an
  active release branch if one exists at the time.

## Scope

- Product: `VITechnologies/projectbuddy`
- Day-to-day feature development happens on the `svelderrainruiz/projectbuddy`
  fork; its `main` branch plays GitFlow's `develop` role. Sync pull requests
  from the fork target this repo's `develop` branch (not `main`).
- `main` remains the single source of truth for what is actually released
  and tagged; `develop` is never released from directly.

## Feature Flow

1. Create feature branches from the fork's `main` (= `develop`).
2. Land feature work into the fork's `main` through a reviewed pull request
   on `svelderrainruiz/projectbuddy`.
3. Sync the fork's `main` to this repo's (`VITechnologies/projectbuddy`)
   `develop` via a branch + pull request targeting `develop` (named either
   `sync-from-fork-<date>` or `sync-upstream-develop-<topic>` in practice;
   both are the same mechanism, just named for the date or the change
   being synced).
4. Delete merged feature branches on the fork after promotion.
5. After a release promotes `develop` to `main` here (Release Flow below),
   sync that same `main` back to the fork via a `sync-upstream-main-vX.Y.Z`
   pull request on `svelderrainruiz/projectbuddy` -- this is what keeps the
   fork's `main` (step 1's starting point) current, and is also why
   `develop` and `main` here are typically already content-identical by
   the time Release Flow step 4 runs.

## Release Flow

1. Create `release/vX.Y.Z` from this repo's `develop`.
2. Stabilize the release only on the release branch (version bump,
   `CHANGELOG.md` entry, any last-mile fixes).
3. Open and merge a pull request `release/vX.Y.Z` -> `main`.
4. Open and merge a pull request `release/vX.Y.Z` -> `develop` (the
   merge-back), only after step 3's promotion to `main` is already merged
   -- **unless `develop` and `main` are already content-identical at this
   point** (verify with `diff <(git show main:CHANGELOG.md) <(git show
   develop:CHANGELOG.md)` or a full tree diff), in which case this step
   is a legitimate no-op: skip opening the merge-back PR rather than
   opening one GitHub would report has no commits to merge.
5. Delete `release/vX.Y.Z` once both merges are complete (or once step 3
   is merged and step 4 was confirmed to be a no-op).
6. Tag `vX.Y.Z` on the resulting `main` commit and publish the GitHub
   Release as a draft first, verify all assets, then publish (immutable
   releases are enabled on this repo: once published, the tag cannot move
   or be deleted/reused, and assets cannot change). **Tag the commit
   actually promoted by step 3/4 -- do not land further PRs directly on
   `main` before tagging** (see `docs/cm/cm-plan.md`'s Status Accounting
   section for a recorded, judged exception where this was done anyway
   for `v0.3.28`, and why).

## Hotfix Flow

1. Create `hotfix/vX.Y.Z` from `main`.
2. Land the hotfix on the hotfix branch only.
3. Open and merge a pull request `hotfix/vX.Y.Z` -> `main`.
4. Open and merge a pull request `hotfix/vX.Y.Z` -> `develop` (the
   merge-back), only after step 3 is already merged.
5. If an active `release/*` branch exists, also open and merge the same
   hotfix into that release branch after step 3 is merged.
6. Delete `hotfix/vX.Y.Z` once all applicable merges are complete.
7. Tag and publish exactly as in Release Flow step 6.
