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

| Gate | Status | Tracking |
|---|---|---|
| CM (incl. GitFlow branch governance) | Addressed by this change (`ADR-0001`) | `VITechnologies/projectbuddy#6` |
| Coverage | FAIL | `VITechnologies/projectbuddy#8` |
| REQ | FAIL | `VITechnologies/projectbuddy#9` |
| ARCH | FAIL (partially addressed by `ADR-0001` + this doc) | `VITechnologies/projectbuddy#10` |
| DOC | FAIL | `VITechnologies/projectbuddy#11` |
| DoD | N/A (optional, not required) | not tracked |

## Living re-certification policy

The stamp above is a **point-in-time record**, not a one-time claim that is
assumed to remain true forever. A scheduled check
(`.github/workflows/standards-compliance-recheck.yml`, weekly) compares the
stamped version above against `repo-standards-review`'s latest published
tag:

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

This policy, and the version stamp format, is specific to this repo for
now (not yet a reusable engine capability offered to other consumers).
