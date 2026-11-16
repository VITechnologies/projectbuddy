# Standards Compliance

This repo is assessed against `svelderrainruiz/repo-standards-review`'s
`release-gate` profile, per operator direction
(`VITechnologies/projectbuddy#7`). This document is the canonical,
machine-checked record of which version of that standard the current
compliance posture was assessed against.

<!-- standards-compliance: repo=svelderrainruiz/repo-standards-review tag=v0.5.0 sha=0cab3803284aafc66f786d7a1114d58fc04fc5e6 -->

| Field | Value |
|---|---|
| Standard | `svelderrainruiz/repo-standards-review` |
| Version (tag) | `v0.5.0` |
| Version (commit SHA) | `0cab3803284aafc66f786d7a1114d58fc04fc5e6` |
| Assessed | 2026-11-16 |
| Profile | `release-gate` |

`v0.5.0` adds the ecosystem wiki standard, `rsr/ecosystem-wiki-standard@1`.
`projectbuddy-wiki` is its reference checker (#318, #319, #320), and this
repository's wiki conforms to it with zero findings. The release does not
change the heuristic `release-gate` scoring.

Re-certification against `v0.5.0` on 2026-11-16 found no regression in the 5
required gates. All still `PASS` at High confidence (99-100%), and DoD remains
the optional `FAIL`.

The re-certification ran the exact steps of
`.github/workflows/standards-assurance-scan.yml` locally against the pinned
`v0.5.0` commit:
- strict wiki assembly;
- the four assembled specifications;
- `run_assurance.py --profile release-gate --mode evidence-pack`.

It first required two wiki fixes, because `Assemble-WikiSpec.py --strict`
had refused the wiki:
- The Software Requirements Specification hub was 17,035 bytes against its
  16,384-byte budget, a breach that began at wiki `b59758d`. Its Glossary
  moved, unchanged, into a child page, and every assembled specification
  stayed byte-identical.
- `Ecosystem.md` became a note.

The workflow itself re-runs on the fork's `standards-review-local` runner
after the next fork-main sync.

## Current gate status (as of the version above)

All 5 required gates now `PASS` at `High` confidence (99-100%), verified
via real CI (`.github/workflows/standards-assurance-scan.yml`), not just
manual assertion. This is reproducible, not a one-time historical claim:
re-run that workflow (`workflow_dispatch`, requires a
`standards-review-local` self-hosted runner -- currently only registered
on the `svelderrainruiz/projectbuddy` fork, not on
`VITechnologies/projectbuddy` itself, so this workflow has never actually
run upstream; the fork's `main` is byte-identical to upstream `main` at
every release boundary, so a fork-side run is genuine evidence for the
upstream repo's actual content) any time to reconfirm the current state
-- it re-derives all 5 gates fresh from a `repo-standards-review` clone
now pinned to this document's own stamped SHA (round-5 semantic-audit
fix: it previously cloned floating `HEAD`, and only failed the run on a
`req`/`arch` regression specifically, silently ignoring a `cm`/
`coverage`/`doc` regression -- both now fixed). This is a distinct
workflow from `standards-compliance-recheck.yml`, which owns the
separate "Compare Stamped Version Against Latest" drift check (whether a
*newer* `repo-standards-review` release exists) and does not itself
re-run the gates.

| Gate | Status | Confidence | Tracking |
|---|---|---|---|
| CM (incl. GitFlow branch governance) | ✅ PASS | High (100%) | `VITechnologies/projectbuddy#6` (closed) |
| Coverage | ✅ PASS | High (99%) | `VITechnologies/projectbuddy#8` (closed) |
| REQ | ✅ PASS | High (100%) | `VITechnologies/projectbuddy#9` (closed) |
| ARCH | ✅ PASS | High (100%) | `VITechnologies/projectbuddy#10` (closed) |
| DOC | ✅ PASS | High (100%) | `VITechnologies/projectbuddy#11` (closed) |
| DoD | FAIL (optional, not required) | 3/5, 60% completion, Med (88%) | not tracked |

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
`VITechnologies/projectbuddy`, which has no `standards-review-local`
runner registered -- though it does have a real, online `vm-provisioner`-
labeled self-hosted runner for VM-provisioning workflows specifically;
"zero private infrastructure state" describes this engine's own
*pre-committed configuration*, not every runner ever registered against
it) will see this job queue and auto-fail once a week
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
