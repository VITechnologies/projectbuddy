# Information Item Map

Satisfies `repo-standards-review`'s DOC gate information-item mapping
requirement (`VITechnologies/projectbuddy#11`).

## Scope

- Product or service: `VITechnologies/projectbuddy`
- Repository: `VITechnologies/projectbuddy` (specs live partly in its wiki,
  see Notes below)
- Baseline: `v0.3.27`
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
| Procedure | `docs/vm-provisioning/procedure.md` | Operator | VM-provisioning workflow/phase change | the procedure matches each workflow's real phase list and inline-documented safety properties |
| Procedure | `docs/sbom-pipeline/procedure.md` | Operator | SBOM-pipeline stage/env-var contract change | the procedure matches both the native and containerized pipelines' real stage lists and documented env-var contracts |
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
