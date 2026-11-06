# Standards Compliance

This repo is assessed against `svelderrainruiz/repo-standards-review`'s
`release-gate` profile, per operator direction
(`VITechnologies/projectbuddy#7`). This document is the canonical,
machine-checked record of which version of that standard the current
compliance posture was assessed against.

<!-- standards-compliance: repo=svelderrainruiz/repo-standards-review tag=v0.2.19 sha=d44f210ded557cda6d4598cdaffe938da51d873e -->

| Field | Value |
|---|---|
| Standard | `svelderrainruiz/repo-standards-review` |
| Version (tag) | `v0.2.19` |
| Version (commit SHA) | `d44f210ded557cda6d4598cdaffe938da51d873e` |
| Assessed | 2026-11-06 |
| Profile | `release-gate` |

## Current gate status (as of the version above)

All 5 gates now `PASS`, verified via real CI
(`.github/workflows/standards-assurance-scan.yml`, run `42201479527`),
not just manual assertion. Re-run that workflow (`workflow_dispatch`)
any time to reconfirm.

| Gate | Status | Tracking |
|---|---|---|
| CM (incl. GitFlow branch governance) | ✅ PASS | `VITechnologies/projectbuddy#6` (closed) |
| Coverage | ✅ PASS | `VITechnologies/projectbuddy#8` (closed) |
| REQ | ✅ PASS | `VITechnologies/projectbuddy#9` (closed) |
| ARCH | ✅ PASS | `VITechnologies/projectbuddy#10` (closed) |
| DOC | ✅ PASS | `VITechnologies/projectbuddy#11` (closed) |
| DoD | N/A (optional, not required) | not tracked |

## Living re-certification policy

The stamp above is a **point-in-time record**, not a one-time claim that is
assumed to remain true forever. A scheduled check
(`.github/workflows/standards-compliance-recheck.yml`, weekly) compares the
stamped version above against `repo-standards-review`'s latest published
tag.

`repo-standards-review` is a private repo. Rather than grant a CI token
broad cross-repo access, this check runs on a **self-hosted runner labeled
`standards-review-local`**, registered against this fork
(`svelderrainruiz/projectbuddy`) specifically, whose own ambient git/gh
credentials already have real, working read access to the private repo (no
per-workflow secret needed). This is a deliberately reusable pattern: anyone
forking this engine can create their own private `repo-standards-review`
instance and register their own `standards-review-local`-labeled runner the
same way, on their own fork, to get the same living compliance check.
Repos without such a runner (including this engine's own public upstream,
`VITechnologies/projectbuddy`, which deliberately carries zero private
infrastructure state) will see this job queue and auto-fail once a week
after GitHub's standard 24-hour no-runner timeout -- expected, and not an
error requiring action there.

What the check does once it runs:

- **If a newer version exists**: the workflow opens or updates a tracking
  issue (labeled `needs-human-decision`, via `New-HumanEscalation.ps1`)
  flagging that re-certification is due. It **never** edits this stamp, the
  gate table above, or re-runs the assurance pipeline automatically --
  compliance could have regressed under a newer version of the standard,
  and that determination is always a human call, not an automated
  overwrite.
- **If the stamped version is still current**: the workflow is a silent
  no-op.
- To re-certify: manually re-run
  `python3 scripts/run_assurance.py . --profile release-gate` against a
  fresh checkout of this repo using the newer `repo-standards-review`
  version, review the result, and update the stamp (and this table) by hand
  as part of that review -- the same way the original stamp above was
  produced.

The version-stamp format and the self-hosted-runner mechanism are a
documented, reusable pattern (see above) for any fork of this engine that
wants its own living compliance infrastructure; this document itself
remains specific to this repo's own current stamp and gate status.
