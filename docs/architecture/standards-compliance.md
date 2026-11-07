# Standards Compliance

This repo is assessed against `svelderrainruiz/repo-standards-review`'s
`release-gate` profile, per operator direction
(`VITechnologies/projectbuddy#7`). This document is the canonical,
machine-checked record of which version of that standard the current
compliance posture was assessed against.

<!-- standards-compliance: repo=svelderrainruiz/repo-standards-review tag=v0.2.23 sha=ba72187ce5f745a1ffea93536cf83982b747819a -->

| Field | Value |
|---|---|
| Standard | `svelderrainruiz/repo-standards-review` |
| Version (tag) | `v0.2.23` |
| Version (commit SHA) | `ba72187ce5f745a1ffea93536cf83982b747819a` |
| Assessed | 2026-11-06 |
| Profile | `release-gate` |

`v0.2.23` added a canonical multi-model semantic-audit mode
(`scripts/semantic_audit.py`), which extracts real standards clause text
live from the local PDF corpus and delegates it to independent models
rather than relying on pattern-match heuristics alone; it does not change
the heuristic `release-gate` scoring logic itself. Re-certification
against `v0.2.23` found no regression in the 5 required gates (all still
`PASS`, High confidence 99-100%) and caught a second instance of the same
self-referential false-positive bug class this document already fixed
once: `CHANGELOG.md`'s own v0.3.22 entry (describing the first fix)
quoted the exact literal CI-context string the DoD rule detects, which
was itself re-triggering a false DoD `PASS` -- rephrased the same way,
without reproducing the matched string, restoring the real, correctly
`FAIL (not required)` DoD state reflected below.

A follow-on full 5-area semantic-audit pass (same `v0.2.23` tooling, 3
independent models) found and fixed a cluster of real documentation/code
defects spanning REQ/ARCH/DOC (see `v0.3.26`'s `CHANGELOG.md` entry), and
surfaced one still-open, high-severity finding needing an operator
decision (`#62`: `provision-ubuntu-vm.yml` has never been successfully
dispatched and references 3 scripts that have never existed). None of
these affect the 5 required gates below -- the heuristic scanner has no
rule that would have caught any of them, which is the entire reason the
semantic-audit capability was built.

A third, verification-focused semantic-audit pass (same 3 models,
explicitly asked to confirm whether `v0.3.26`'s fixes actually held up,
not just re-describe the original findings) found that most fixes were
genuinely SOLID, but several single-pass corrections had residual gaps
or were incomplete (a `trap` installed after, not before, a credential
file's creation; a corrected claim left standing in the wiki Architecture
Packet after being fixed only in `README.md`/`docs/`; an activation
precondition described as enforced when it is actually only a documented
convention) -- see `v0.3.27`'s `CHANGELOG.md` entry. This reinforces the
practical value of a dedicated verification pass after any
semantic-audit-driven fix round, not just the initial audit.

## Current gate status (as of the version above)

All 5 required gates now `PASS` at `High` confidence (99-100%), verified
via real CI (`.github/workflows/standards-assurance-scan.yml`), not just
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
| REQ | ✅ PASS | High (99%) | `VITechnologies/projectbuddy#9` (closed) |
| ARCH | ✅ PASS | High (100%) | `VITechnologies/projectbuddy#10` (closed) |
| DOC | ✅ PASS | High (100%) | `VITechnologies/projectbuddy#11` (closed) |
| DoD | FAIL (optional, not required) | 3/5, 60% completion, Med (90%) | not tracked |

Overall confidence: High (100%); 0 contradictions.

DoD is intentionally not one of this repo's 5 required gates (per the
tracking column above) -- `repo-standards-review`'s own rubric only awards
a full `PASS` when a literal CI status-check context (the gate's own name
joined to its job id by a slash) is visible, which this repo does not
have and is not required to add. This repo's `CONTRIBUTING.md` does carry
a genuine, itemized Definition of Done checklist (added alongside this
re-certification), which is why the score moved from 0/5 (0%, no DoD
content at all) to 3/5 (60%, itemized checklist present but no enforcing
CI gate) rather than being a fabricated pass.

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
