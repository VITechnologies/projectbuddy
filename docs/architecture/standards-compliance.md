# Standards Compliance

This repo is assessed against `svelderrainruiz/repo-standards-review`'s
`release-gate` profile, per operator direction
(`VITechnologies/projectbuddy#7`). This document is the canonical,
machine-checked record of which version of that standard the current
compliance posture was assessed against.

<!-- standards-compliance: repo=svelderrainruiz/repo-standards-review tag=v0.2.20 sha=63eecc2e8e1f54450b21c9a90d0c132cf6b4c06a -->

| Field | Value |
|---|---|
| Standard | `svelderrainruiz/repo-standards-review` |
| Version (tag) | `v0.2.20` |
| Version (commit SHA) | `63eecc2e8e1f54450b21c9a90d0c132cf6b4c06a` |
| Assessed | 2026-11-06 |
| Profile | `release-gate` |

`v0.2.20` added a `confidence_score` (0-100) alongside every existing
High/Med/Low label, and replaced the DoD gate's old binary
PASS/N/A check with a graduated 0-5 score and `completion_pct`, matching
the REQ/ARCH/TEST/CM/DOC rubric pattern. Re-certification against this
version found no regression in the 5 required gates (all still `PASS`
with `High`/100% confidence) and is reflected below.

## Current gate status (as of the version above)

All 5 required gates now `PASS` at `High` confidence (100%), verified via
real CI (`.github/workflows/standards-assurance-scan.yml`), not just
manual assertion. This is reproducible, not a one-time historical claim:
re-run that workflow (`workflow_dispatch`, requires a
`standards-review-local` self-hosted runner) any time to reconfirm the
current state -- its own "Compare Stamped Version Against Latest" step
always re-derives the answer fresh rather than relying on any past run's
recorded result.

| Gate | Status | Confidence | Tracking |
|---|---|---|---|
| CM (incl. GitFlow branch governance) | ✅ PASS | High (100%) | `VITechnologies/projectbuddy#6` (closed) |
| Coverage | ✅ PASS | High (100%) | `VITechnologies/projectbuddy#8` (closed) |
| REQ | ✅ PASS | High (100%) | `VITechnologies/projectbuddy#9` (closed) |
| ARCH | ✅ PASS | High (100%) | `VITechnologies/projectbuddy#10` (closed) |
| DOC | ✅ PASS | High (100%) | `VITechnologies/projectbuddy#11` (closed) |
| DoD | FAIL (optional, not required) | 3/5, 60% completion, Med (65%) | not tracked |

DoD is intentionally not one of this repo's 5 required gates (per the
tracking column above) -- `repo-standards-review`'s own rubric only awards
a full `PASS` when a literal `DoD Gate / dod` CI status-check context
exists, which this repo does not have and is not required to add. This
repo's `CONTRIBUTING.md` does carry a genuine, itemized Definition of
Done checklist (added alongside this re-certification), which is why the
score moved from 0/5 (0%, no DoD content at all) to 3/5 (60%, itemized
checklist present but no enforcing CI gate) rather than being a
fabricated pass.

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
