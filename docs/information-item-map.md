# Information Item Map

Satisfies `repo-standards-review`'s DOC gate information-item mapping
requirement (`VITechnologies/projectbuddy#11`).

## Scope

- Product or service: `VITechnologies/projectbuddy`
- Repository: `VITechnologies/projectbuddy` (specs live partly in its wiki,
  see Notes below)
- Baseline: `v0.3.31`
- Date: 2026-11-07
- Status: current (re-synced after each semantic-audit round; see
  `docs/architecture/standards-compliance.md` for the living
  re-certification policy)
- Owner: svelderrainruiz

## Information Items

| Item Type | Current Path | Owner | Trigger | Proving Evidence |
| --- | --- | --- | --- | --- |
| Software Specification | wiki: `Software-Requirements-Specification` | Engineering | new/changed behavior (spec-first) | `REQ-###` IDs and fit criteria stay current; `standards-assurance-scan.yml` scans it on every run |
| Traceability | wiki: `Requirements-Traceability-Matrix` | Engineering | new requirement or test | each `REQ-###` row links to a `TEST-###` and a real `CodeRef` |
| Architecture | wiki: `Architecture-Packet` | Engineering | architecture change | all 4 views and the ADR Index stay current |
| Decision Record | `docs/architecture/adr/ADR-0001-adopt-gitflow-for-release-governance.md` | Operator | a significant, retained technical decision | a new `ADR-NNNN` file is added, never edited after acceptance |
| Plan | `docs/cm/cm-plan.md` | Operator | configuration/release-process change | change control and status accounting sections stay current |
| Procedure | `docs/release/release-procedure.md` | Operator | release/hotfix flow change | the procedure matches the live GitFlow branch rules in practice |
| Procedure | `docs/vm-provisioning/procedure.md` | Operator | VM-provisioning workflow/phase change, or a packager option, runner scan, or box receipt change | the procedure matches each workflow's real phase list and inline-documented safety properties; `scripts/tests/test_package_vagrant_box.py` fails if the packaging section's options, scan rule, or `runnerScan` fields drift from the code |
| Procedure | `docs/vm-provisioning/ubuntu-lane-box.md` | Operator | lane profile, readiness, or box receipt field, command, refusal, or exit code change | `scripts/tests/test_lane_box_docs.py` fails if the documented commands, profile fields, receipt fields, schemas, defaults, or exit codes drift from the code |
| Procedure | `docs/vm-provisioning/ubuntu-lane.md` | Operator | lane workload declaration field, plan refusal rule, plan field, plan step, teardown allow-list, command, option, receipt field, outcome, timeout, or exit code change | `scripts/tests/test_lane_docs.py` fails if the documented declaration fields, refusal rules, plan fields, steps, phases, allow-list, commands, options, receipt fields, outcomes, paths, timeouts, or exit codes drift from the code |
| Procedure | `docs/vm-provisioning/ubuntu-vagrant-clone.md` | Operator | clone profile field or test, runner unit query or policy, clone receipt field, schema, path, timeout, option, or exit code change | `scripts/tests/test_vagrant_clone_docs.py` fails if the documented profile fields, tests, runner unit queries, policies, receipt fields, schemas, paths, options, or exit codes drift from the code |
| Procedure | `docs/sbom-pipeline/procedure.md` | Operator | SBOM-pipeline stage/env-var contract change | the procedure matches both the native and containerized pipelines' real stage lists and documented env-var contracts |
| Procedure | `docs/approval-gate/procedure.md` | Operator | approval-gate command, registry key, readiness finding, or refusal reason change | `scripts/tests/test_approval_gate_docs.py` fails if the documented findings, refusal reasons, registry keys, or example registry drift from the code |
| Compliance Stamp | `docs/architecture/standards-compliance.md` | Operator | `repo-standards-review` publishes a new version | `standards-compliance-recheck.yml` flags drift weekly; stamp updated only after human review |

## Notes

- The Software Specification, Traceability Matrix, and Architecture
  Packet live in this repo's wiki (`VITechnologies/projectbuddy.wiki`),
  not this `docs/` tree, per operator direction (spec-driven design,
  Obsidian-vault-compatible via native `[[wiki-link]]` syntax, which
  GitHub's wiki renderer supports but the main repo's markdown renderer and
  `lychee` link-checker do not -- see the wiki's own `Home` page).
  `standards-assurance-scan.yml` copies them into `docs/requirements/` and
  `docs/architecture/overview.md` at scan time so the compliance tooling
  can see them; that copy is ephemeral and never committed here.
- This table is updated whenever an information item's path, owner, or
  proving evidence changes -- not on a fixed schedule.
- The wiki may split each specification hub into linked, ID-addressable
  pages (one page per `SYS-REQ-###`, `REQ-###`, and `TEST-###`, and one per
  Architecture Packet section) so Obsidian's graph shows the trace
  relationships. Before staging, the scan rebuilds the System
  Specification, Software Specification, Traceability Matrix, and
  Architecture Packet hubs with `scripts/Assemble-WikiSpec.py`, which fails
  closed if any page content would be left out; an unsplit wiki assembles
  byte-for-byte unchanged. The System Specification is staged as
  `docs/requirements/syrs.md`.
- The scan runs the assembler with `--strict --max-page-bytes 16384`, so it
  enforces the wiki `Home` page's link conventions: unpiped `[[Page-Name]]`
  links with no `|` alias and no `#heading` anchor, every link resolving to
  a wiki page, every ID mention linked unless it is in code formatting, and
  pages capped at 16 KiB (16,384 bytes). A violation fails the scan before
  anything is staged.
- Whenever assembly succeeds, even if a gate then fails, the scan run
  publishes the four files it staged as a generated, non-authoritative view
  for whole-table review: the job summary shows each file's SHA-256 and the
  assembled Traceability Matrix, and the `assembled-wiki-spec` artifact holds
  all four exactly as scored. In the summary copy, written by the
  assembler's `--view` option, wikilinks are rendered as Markdown links to
  the wiki page that holds each row or section, because job summaries do not
  render wikilinks. Both need a GitHub sign-in to view, even for this public
  repository. Edit the wiki pages, never this output.
