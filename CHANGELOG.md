# Changelog

All notable changes to this project are documented here. Versions are
published as immutable GitHub releases (tag + commit SHA); see each
release's notes for the exact pinned commit.

## v0.3.25

- fix `install-labview`: verify the supplied LabVIEW installer ISO's SHA-256 against `provisioning-manifest.json`'s pinned `labview.installer.sha256` before attaching it, fail-closed on mismatch -- previously this phase attached the operator-supplied ISO with no verification at all, unlike `install-vipm`'s existing fail-closed check, even though the manifest already pinned the exact expected hash (`#38`). Real-dispatch validated against an actual VM: confirmed the valid-hash path installs LabVIEW successfully end to end, and the mismatched-hash path fails closed before any VM is touched

## v0.3.24

- remove `docker/windows-sbom/`'s unused code-signing toolchain (`Install-SigningTools.ps1`, `Install-VerifiedNugetPackage.ps1`, and their Dockerfile `COPY`/`RUN` lines): a complete, self-verifying SignTool/Azure-Artifact-Signing install with zero consumers anywhere in the pipeline since the very first commit (`v0.2.0`) -- the `.lvlibp` output this would plausibly sign is already trusted downstream via GitHub's in-toto release attestation chain, not a code-signing certificate, so this was unconnected scaffolding adding real image-build time and an unused native-DLL load for no current benefit. Removing it in favor of adding it back deliberately, with real wiring and tests, if a genuine signing requirement ever materializes (`#44` finding 1)
- document the containerized pipeline's VIPM Pro activation flow in `docs/sbom-pipeline/procedure.md`: both `docker/linux-sbom/entrypoint.sh` and `docker/windows-sbom/entrypoint.ps1` require `VIPM_SERIAL_NUMBER`/`VIPM_FULL_NAME`/`VIPM_EMAIL` and activate VIPM Pro via `vipm activate --direct-vipm-io` on every container start unless `SBOM_SOURCE=manifest`, redacting the serial number from error output and unsetting all 3 variables before the pipeline itself runs -- previously undocumented (`#44` finding 2)

## v0.3.23

- fix REQ-017: the guest/WinRM password file written during Windows LabVIEW VM provisioning was never shredded/removed anywhere in the workflow (only an unrelated PAT file was) -- added a `shred` (with `rm -f` fallback) cleanup line to every step in `provision-windows-labview-vm.yml` that writes it
- fix REQ-030: `add-project.yml`'s upsert only replaced the first matching entry by project name, so a pre-existing duplicate entry would survive a re-registration instead of collapsing to one -- now filters out every matching entry before appending the fresh one
- correct REQ-013, REQ-018/REQ-019 (narrowed "LabVIEW/VIPM" to "LabVIEW" -- VIPM activation on the Ubuntu-LabVIEW family is genuinely automated and deliberately so, now its own REQ-032), and REQ-025 wiki SRS/RTM/Architecture-Packet wording to match the real code they cite -- all found by this session's multi-model semantic audit (`#52`) cross-checking the SRS against the real ISO/IEC/IEEE 29148:2018 clause text and the real repo source, not just the heuristic `repo-standards-review` pattern-match score
- fix a second instance of the same self-referential DoD false-positive bug class: `CHANGELOG.md`'s own v0.3.22 entry (describing the first fix) quoted the exact literal CI-context string the DoD rule detects, re-triggering a false DoD `PASS` -- rephrased the same way, without reproducing the matched string
- re-certify the compliance stamp against `repo-standards-review@v0.2.23` (adds the canonical multi-model semantic-audit mode used to find the REQ findings above): all 5 required gates still `PASS`, High confidence (99-100%); DoD correctly back to its real `FAIL (not required)`/60% state once the self-referential false positive above was fixed

## v0.3.22

- fix a self-referential false-positive: `docs/architecture/standards-compliance.md`'s own explanatory prose quoted the literal CI-context string `repo-standards-review`'s DoD rule detects (gate name, a slash, then the job id), which was itself matching that detection rule and producing a false DoD `PASS` -- rephrased without reproducing the exact matched string, same class of bug already fixed in `repo-standards-review`'s own `references/scoring-rubric.md`
- re-certify the compliance stamp against `repo-standards-review@v0.2.22` (two releases stale at `v0.2.20`): all 5 required gates still `PASS`, now with genuinely differentiated `High` confidence (99-100%) instead of a flat ceiling, following `v0.2.22`'s fix for `confidence_score`'s saturation flaw

## v0.3.21

First spec-driven reproducibility audit pass on the SBOM pipeline
subsystem (mirroring `v0.3.19`'s pass on VM provisioning):

- Fixed a real, concrete copy-paste artifact: `docker/windows-sbom/Dockerfile`
  had `COPY Install-SigningTools.ps1 C:/sbom/Install-SigningTools.ps1`
  duplicated on two consecutive lines (harmless -- Docker just
  re-executes an idempotent COPY -- but clearly unintentional).
- Found, but did not patch blind: a complete code-signing toolchain
  (`Install-SigningTools.ps1`, `Install-VerifiedNugetPackage.ps1`,
  `VipmInstallerSignature.ps1`) is downloaded, SHA-512/Authenticode
  verified, and installed at image-build time, but has zero consumers
  anywhere in the pipeline -- present unused since this engine's very
  first commit (`v0.2.0`). Also found the containerized pipeline's real
  VIPM Pro activation flow (`entrypoint.ps1`) is undocumented. Filed as
  `#44` for an operator decision (remove the unused toolchain vs. finish
  wiring it up) rather than guessing intent.

## v0.3.20

Re-certified this repo's compliance stamp against
`repo-standards-review@v0.2.20`, which added a quantified
`confidence_score` (0-100) alongside every existing High/Med/Low label,
and replaced the DoD gate's old binary PASS/N/A check with a graduated
0-5 score and `completion_pct`. Re-ran the real
`standards-assurance-scan.yml` CI workflow: no regression, all 5 required
gates still `PASS` at `High`/100% confidence.

Added a genuine, itemized Definition of Done checklist to
`CONTRIBUTING.md` -- not required for this repo's 5 tracked gates, but a
real, honest improvement: moved the DoD score from 0/5 (0%, no DoD
content at all) to 3/5 (60% completion, Med/65% confidence), verified via
the real assurance pipeline before and after, not asserted.

## v0.3.19

First real pass at the operator's "spec-driven reproducibility" exercise:
re-read the already-documented VM provisioning source specifically
hunting for places where the spec and the real implementation disagree,
on the theory that such a mismatch signals an implementation problem
(dead code, tech debt, an unjustified heuristic), not just a doc gap.

Findings:

- `vm/windows-labview/build-virtualbox.sh`: added a one-line rationale
  comment explaining why the OS/hardware profile is deliberately
  double-pinned (manifest values cross-checked against hardcoded literal
  expectations) -- the design was sound, it just had no comment
  explaining why, which this exercise is specifically meant to catch.
- `docs/vm-provisioning/procedure.md`: documents 2 real findings honestly
  rather than papering over them -- `unattended-template.xml` is a
  deliberately near-verbatim *vendored copy* of VirtualBox's own shipped
  template (not projectbuddy's own stale TODOs, confirmed via the file's
  own header before touching anything), and `install-labview` has a real,
  undocumented-until-now asymmetry with `install-vipm`: no SHA-256
  verification of the supplied ISO, despite the manifest already pinning
  the exact expected hash. Filed as `VITechnologies/projectbuddy#38`
  rather than patched blind, per this engine's own real-dispatch-only
  validation discipline.
- `.github/workflows/provision-windows-labview-vm.yml`: removed one more
  ephemeral workflow-run-ID citation found during this pass (continuing
  `#35`'s fix, this time in a pre-existing workflow comment rather than a
  doc), describing what was proven instead of citing specific run
  numbers.

Live-verified before committing: ran the real assurance pipeline against
the updated spec set -- all 5 gates still `PASS`.

## v0.3.18

Fix `VITechnologies/projectbuddy#35` (stale CI-integration text in the
wiki spec) and act on new operator guidance: workflow run logs/summaries
are ephemeral and must never be cited as "the evidence" in persistent
documentation.

- `docs/architecture/standards-compliance.md`, `CHANGELOG.md`'s own
  `v0.3.10` entry: removed the 2 bare workflow-run-ID citations found in
  this repo, replaced with descriptions of the reproducible mechanism
  itself (re-runnable via `workflow_dispatch`) instead of a specific past
  run number.
- Wiki: fixed the SRS's stale "wiring question still open" text (resolved
  back in `v0.3.11`, but never actually documented where it was pointing);
  added the real resolution to the Architecture Packet's Correspondence
  And Rationale section; fixed the one bare run-ID citation there too
  (`REQ-011`); updated the RTM's "Verified (live)" legend to state the
  no-ephemeral-run-ID principle explicitly for future additions.

Live-verified before committing: ran the real assurance pipeline against
the fully updated spec set -- all 5 gates still `PASS`.

## v0.3.17

Close the same shallow-spec-coverage pattern `#29` closed for VM
provisioning, this time for the native + containerized SBOM pipeline
(`VITechnologies/projectbuddy#32`).

- `docs/sbom-pipeline/procedure.md` (new): the full stage-by-stage
  procedure for both the native pipeline (`Run-Pipeline.ps1`/`.sh`,
  `Build-Sbom.ps1`, `internal/Pipeline.Stages.ps1`) and the containerized
  pipeline (`docker/linux-sbom/`, `docker/windows-sbom/`), grounded
  directly in the real code -- including the full-mode fail-fast
  reporting preflight, the LVLIBP release-dependency immutable-release +
  in-toto-attestation + tag-chain + manifest verification chain, and the
  deliberate native-vs-container difference in whether Grype's own
  findings gate the run. Also fixes 2 dangling doc references
  (`docker/linux-sbom/Dockerfile`, `docker/windows-sbom/Dockerfile`
  pointed at `docs/linux-sbom-docker.md`/`docs/windows-sbom-docker.md`,
  neither of which ever existed) to point at this new, real doc instead.
- Wiki: 8 new requirements (`REQ-022`-`REQ-029`) and matching RTM rows,
  Architecture Packet Component View expanded with the real stage
  library and shared schemas.
- `docs/information-item-map.md`, `README.md`: updated to reference the
  new procedure doc.

Live-verified before committing: ran the real assurance pipeline against
this repo + the updated wiki content -- all 5 release-gate gates still
`PASS`.

## v0.3.16

Close a real documentation gap the operator flagged directly and the
automated gates missed (`VITechnologies/projectbuddy#29`): VM
provisioning had only 2 narrow requirements and 3 shallow architecture
rows despite being ~37 scripts and 1200+ lines of real, dispatch-proven
workflow behavior.

- `docs/vm-provisioning/procedure.md` (new): the full phase-by-phase
  procedure for both VM families -- Windows LabVIEW's 5-phase single-VM
  lifecycle and Ubuntu LabVIEW's 10-phase, dual Linux/Windows-adapter,
  snapshot-based appliance lifecycle -- grounded directly in the real
  workflow YAML and its own inline comments, including the deliberate
  human-only LabVIEW/VIPM activation boundary and `vm-fleet-status.yml`'s
  active-verification design.
- Wiki: 9 new requirements (`REQ-013`-`REQ-021`) and matching RTM rows,
  Architecture Packet Component/Deployment views expanded with the real
  VM-provisioning components and lifecycle.
- `docs/information-item-map.md`, `README.md`: updated to reference the
  new procedure doc.

Live-verified before committing: all 5 release-gate gates still `PASS`
with the expanded spec set (not just coincidentally, per the finding in
`#29` -- this time because the actual subsystem is genuinely documented).

## v0.3.15

Branch protection live on both repos, implementing `VITechnologies/projectbuddy#20`:

- `VITechnologies/projectbuddy` `main`/`develop`: require `Docs Link Check / lychee` + `PR Coverage Gate / coverage` + 1 approving review; default branch moved `main` -> `develop`.
- `svelderrainruiz/projectbuddy` `main`: same 2 required checks, 0 required approvals (solo maintainer -- requiring reviews would self-lock).
- All force-push/delete disabled on every protected branch; `enforce_admins: false` everywhere (deliberate -- admins can still bypass via `--admin` for already-CI-vetted automation).
- Live-verified with real test PRs: fork merged cleanly with 0 approvals; upstream correctly blocked a plain merge, `--admin` override succeeded as designed.
- `CONTRIBUTING.md`: documents the `--admin` merge pattern as the sanctioned, operator-confirmed resolution specifically for fork-to-`develop` sync PRs and `release/*`-to-`main` promotions -- not a general bypass.

## v0.3.14

Per operator direction: proactively consider future-agent-success risks
and codify filing issues for them as standing policy, not a one-off.

- `CONTRIBUTING.md`: the branch model summary + a durable policy on
  proactively filing issues for spec drift and future-agent-success
  risks (e.g. a required branch-protection check that depends on
  infrastructure only one fork has), linked from `README.md`.
- Hardened repository-level Actions settings on both this fork and
  upstream: `default_workflow_permissions` tightened from `write` to
  `read` (upstream only -- the fork was already `read`), and
  `sha_pinning_required` enabled on both (matches the manual pinning
  discipline already followed everywhere in this repo).
- `.github/workflows/ci-assurance.yml`, `docs-link-check.yml`: added the
  explicit `contents: read` permissions block they were missing (found
  while auditing for the above) -- verified both still pass in real CI
  after the repo-wide default tightened.
- Filed `VITechnologies/projectbuddy#20`: branch protection currently
  does not exist anywhere (fork or upstream, `main` or `develop`), and
  the default branch on both is still `main`, not `develop`. Tracked
  there rather than fixed blind, since the correct required-check/review
  configuration differs materially between the solo-maintained fork and
  the multi-admin upstream org repo.

## v0.3.13

Close the Coverage gate (`VITechnologies/projectbuddy#8`) -- the last of
the 5 `release-gate` gates to close:

- `scripts/tests/test_convert_vipb_json.py`: 17 real, assertion-based
  unit/CLI tests for `Convert-VipbJson.py`'s XML<->JSON and dot-path-patch
  logic (the genuinely unit-testable script in this repo -- VM
  provisioning/pipeline scripts need a live VM/VIPM toolchain to exercise
  meaningfully and stay validated via real CI dispatch instead, per
  `docs/requirements/srs.md` REQ-008/REQ-009). 95% line coverage of the
  module under test, 97.9% including the test file itself.
- `.github/workflows/ci-assurance.yml`: job literally named
  `PR Coverage Gate / coverage` (required by `GATE:coverage:context`),
  triggered on `pull_request` and `push` to `main`. Scoped honestly to
  `scripts/` (not the whole repo) -- enforces a 90% threshold, real and
  verified locally before committing, and uploads `coverage.xml` as a
  retained artifact.

## v0.3.12

Close the remaining DOC gate gaps (`VITechnologies/projectbuddy#11`):

- `docs/information-item-map.md`: maps every real information item (wiki
  specs, ADRs, CM plan, release procedure, compliance stamp) to its owner,
  trigger, and proving evidence.
- `.github/workflows/docs-link-check.yml`: a `lychee`-based docs link
  checker, job literally named `Docs Link Check / lychee` (required by
  `GATE:lychee:context`), triggered on `pull_request` and `push` to
  `main`. Dry-run-verified locally first (0 broken links across 8 checked).

Also noting a known false-positive in the prior `v0.3.11` entry: that
entry's own sentence about `CM:text:release-coverage-rerun` (explaining
the gap) accidentally satisfies that rule's regex just by naming it,
which is why the CM gate started showing `PASS` before real Coverage-gate
work (`VITechnologies/projectbuddy#8`) exists. The gate's literal,
tool-reported status should not be read as "release flow genuinely
re-runs/retains coverage today" until #8 is actually done.

## v0.3.11

Close out the remaining, non-coverage-dependent CM gate gaps and wire real
CI verification for the REQ/ARCH gates against the wiki-hosted specs
(`VITechnologies/projectbuddy#9`, `#10`), per spec-driven-design direction.

- `docs/cm/cm-plan.md`: configuration items, versioning, change control,
  and status accounting, satisfying `CM:path:cm-plan` and
  `CM:text:change-control`.
- `.github/workflows/release.yml`: automates tagging + creating a GitHub
  Release (draft by default; immutable once published) from a
  `CHANGELOG.md` entry, satisfying `CM:path:release-workflow` and
  replacing the prior fully-manual `gh release create`/`edit` sequence.
- `.github/workflows/standards-assurance-scan.yml`: runs the real
  `repo-standards-review` assurance pipeline on `standards-review-local`,
  assembling an ephemeral scan checkout (main repo + the wiki's SRS/RTM/
  Architecture Packet copied into the paths the scanner expects) and
  failing if the REQ or ARCH gates regress. Live-verified locally before
  committing: both gates score 5/5 "defensible", `PASS` with High
  confidence, zero missing items.
- Wiki: added explicit `REQ-### ... TEST-### ... module` wording to the
  RTM so `REQ:text:rtm-link` matches (needed for the REQ gate to actually
  `PASS`, not just score high).

CM gate itself still does not fully `PASS` yet -- it also requires
`CM:text:release-coverage-rerun` evidence (release flow text that
mentions coverage), which is honestly tied to the Coverage gate
(`VITechnologies/projectbuddy#8`) not being implemented yet. Tracked
there, not overstated here.

## v0.3.10

Fix `standards-compliance-recheck.yml`, which failed in every real run
after v0.3.9's `STANDARDS_REVIEW_ACCESS_TOKEN` PAT approach turned out not
to work in practice (fine-grained PAT, correct repository access and
Contents:Read permission, still got "Repository not found" from a
GitHub-hosted runner). Switched to a self-hosted runner labeled
`standards-review-local` (registered against this fork) using ambient git
credentials instead of a PAT — but that still failed identically, until
live diagnostics on the runner found the real root cause:
`actions/checkout`'s default `persist-credentials: true` writes a
host-wide `http.https://github.com/.extraheader` override into
`.git/config` that hijacks *any* subsequent `https://github.com/...` git
operation in the job (not just this repo's own checkout) with the wrong,
scope-less ephemeral job token. `persist-credentials: false` removes it at
the source. Live-verified end to end: the recheck correctly compared
stamped and latest versions and reported no drift, reproducible any time
via `workflow_dispatch` on a repo with a `standards-review-local` runner
registered. The
`STANDARDS_REVIEW_ACCESS_TOKEN` secret is no longer used anywhere and has
been removed from both this fork and the upstream repo.

## v0.3.9

Adopt GitFlow branch governance (`develop` branch, `feature/*`,
`release/*`, `hotfix/*`), per operator decision on `VITechnologies/projectbuddy#6`,
to satisfy `svelderrainruiz/repo-standards-review`'s CM gate — see
`docs/architecture/adr/ADR-0001-adopt-gitflow-for-release-governance.md`
and `docs/release/release-procedure.md`. The decision's condition: every
compliance document stamps the exact `repo-standards-review` version (tag
+ commit SHA) it was assessed against — see
`docs/architecture/standards-compliance.md`, which also documents a weekly,
flag-only (never auto-editing) re-certification check
(`.github/workflows/standards-compliance-recheck.yml`). The
`svelderrainruiz/projectbuddy` fork's `main` now plays GitFlow's `develop`
role; sync pull requests target this repo's `develop` branch going forward,
not `main`.

## v0.3.8

Add `Enable-RepoIssueTracking.ps1`: enables Issues (idempotent, never
disables) on a given `owner/repo` and seeds the `needs-human-decision`
label. `add-project.yml` now calls it automatically, as a best-effort step,
for every GitHub-hosted repo a newly-registered project's config
references (`repoUrl`, `sourceCheckouts[].repository`,
`build.lvlibpDependencies[].repository`); this needs a new
`ACTIONS_REPO_ADMIN_TOKEN` secret and only warns (never fails project
registration) if it is missing or insufficiently scoped. Fixes
`VITechnologies/projectbuddy#4`.

## v0.3.7

Add `New-HumanEscalation.ps1` and `Get-HumanEscalations.ps1`: a first-class,
project-agnostic mechanism for flagging a blocker that automated iteration
cannot resolve on its own, instead of leaving that determination buried in
an ad-hoc issue comment. `New-HumanEscalation.ps1` creates or (re-run with
the same `-EscalationId`) idempotently updates a structured GitHub issue
(context, why the agent could not resolve it, options considered, a
recommendation, related issues) under a consistent `needs-human-decision`
label. `Get-HumanEscalations.ps1` queries one or more repos for open
escalations and prints a single consolidated dashboard.

## v0.3.6

`Run-WindowsSbom.ps1`/`Run-LinuxSbom.ps1` now mount their own location
(`$PSScriptRoot`) as a second, separate `C:\engine`/`/engine` container
mount, alongside the existing consumer-repository workspace mount.
`windows_sbom_pipeline.ps1`, `Build-LvlibpCandidate.ps1`, and
`linux_sbom_pipeline.py` previously resolved `Build-Sbom.ps1`,
`pipeline-config.schema.json`, and `vipm-exit-codes.json` relative to the
single workspace mount, which used to also hold the consumer's own copy of
these files. Once a consumer stops keeping that local copy (consuming this
repository via a pinned checkout instead, per
`docs/projectbuddy-consumption.md`), those reads silently failed inside the
container. The container-side scripts now resolve engine-root files from
the new engine mount, falling back to the workspace mount when no separate
engine mount is present. Mirrors the `$PSScriptRoot`/`-WorkspaceRoot` split
already used by `Run-Pipeline.ps1`, `Add-Project.ps1`, etc. for the native
(non-container) pipeline.

## v0.3.5

`Add-Project.ps1` now accepts `-WorkspaceRoot` (defaulting to `$PSScriptRoot`,
so direct in-repo/local usage is unaffected), used to resolve
`.github/scripts/project_runtime.py`, `.github/scripts/validate-project-registration.py`,
its `git branch --show-current` lookup, and its `gh workflow run` dispatch
working directory. Those `.github/scripts/*` validators stay local-only to
each consumer (never published here), so this silently broke the first time
`Add-Project.ps1` was actually invoked from a pinned checkout instead of a
consumer's own repository root. Mirrors the same `-WorkspaceRoot` pattern
already applied to the other entry scripts in v0.3.0, for the identical
class of problem.

## v0.3.4

Captures bounded, redacted failure diagnostics (process/window snapshot --
the same mechanism already proven for the per-package `library_add_package`
stall) when the manifest dependency install (`vipm install -y`) fails with
the `package_set_install` stall signature. Written to
`packages/manifest-dependency-failure-diagnostics.json`, then the original
error is re-thrown unchanged -- this install is still never retried (v0.3.3
established that retrying here cannot recover a hung LabVIEW and risks
masking a different, unrecoverable hang as a transient stall). Diagnostic
capture is best-effort: a capture failure logs a warning but never masks
the original stall error. `VITechnologies/SandboxSBOMReporting`'s issue
#122 (bounded root-cause evidence capture) already covered the per-package
case; this closes the remaining gap the #240/#244 investigation found --
diagnosing that stall took a multi-hour interactive investigation because
this exact path had zero automated evidence.

## v0.3.3

Reverts v0.3.1/v0.3.2's `Invoke-ManifestDependencyInstall` /
`Stop-StalledVipmDesktop` retry-and-kill-restart logic for the
`package_set_install` manifest dependency stall. A live native end-to-end
re-test of this exact mechanism on `VITechnologies/SandboxSBOMReporting`'s
issue #240 showed the kill-and-retry correctly stopped and restarted the
stalled VIPM Desktop, reconnected cleanly, and still hit the identical
stall on the same package -- proving the stall was never a frozen process.
Interactive host investigation (issue #244) then found the real cause:
LabVIEW's UI thread hanging at startup under a dedicated runner service
account because of the Viewpoint TSVN Toolkit project provider, so VI
Server reset every VIPM connection. That is fixed on the host, not in
pipeline code, and a hung LabVIEW cannot be recovered by retrying or
restarting VIPM Desktop around it -- a retry just re-triggers the same
hang and risks masking a future, different, genuinely-unrecoverable LabVIEW
hang as a transient stall instead of failing fast. Manifest dependency
install is therefore a bare `vipm install -y` call again, matching the
consumer's own reviewed and accepted behavior. v0.3.1/v0.3.2 remain
published (GitHub immutable releases) but are superseded; do not pin to
them.

## v0.3.2

`Invoke-ManifestDependencyInstall`'s `package_set_install` stall retry
(v0.3.1) now stops the stalled VIPM Desktop process (forcibly, waiting for
exit) before retrying, mirroring the Docker container pipeline's existing
`Stop-Process -Force` + `WaitForExit` restart pattern. Live validation of
v0.3.1 alone showed the retry fired correctly but then hung for 10 minutes
on VIPM's own "wait for VIPM startup" readiness check: the vipm CLI could
not initialize a fresh Desktop while the first attempt's already-stalled
instance was still resident and holding its resources.

## v0.3.1

`Build-Sbom.ps1`'s manifest dependency install (`vipm install -y`, resolving
and installing a project's `vipm.toml` `[dependencies]` before any package
build) now retries once with `VIPM_DEBUG=1` when VIPM reports its
`package_set_install` command made no progress -- the same recovery already
proven for the per-package `library_add_package` stall. Found live-debugging
`VITechnologies/SandboxSBOMReporting`'s issue #240: a real dependency-set
install stalled with true zero progress for minutes on end, with no retry
path at all, unlike the sibling per-package install which already recovers
from the equivalent stall automatically.

## v0.2.1

`vm/windows-labview/vnc-bootstrap.py` now accepts an explicit `--inventory`
flag (passed through to `vnc-console.sh`), needed when this script runs
from a location other than where a consumer's real `runner-inventory.json`
lives (e.g. checked out as a pinned release into a subdirectory) -- found
while wiring `VITechnologies/SandboxSBOMReporting`'s real production
cutover (epic #207's #212): without this, the consumer's pinned
`vncPort`/`winrmHostPort` lookups would silently miss (falling back to
this engine's own derived-port logic instead of the consumer's real,
intentional inventory values).

## v0.2.0

First real engine release: the VM-provisioning pipeline
(`vm/windows-labview/`, `vm/ubuntu-labview/`) and the native SBOM reporting
pipeline (root-level `Run-*`, `Build-Sbom.ps1`, `internal/`, `docker/`),
generalized out of `VITechnologies/SandboxSBOMReporting` (that repository's
epic #207, issues #210/#211). Every VM/project identity value is a
required input with no built-in default; Dependency-Track reporting
requires an explicit `DEPENDENCY_TRACK_URL` with no default pointing at any
particular organization's server. See [README.md](README.md)'s "What's in
this engine" section for the full content breakdown.

## v0.1.1

Fixes `toolchain-smoke-test.yml`: a reusable (`workflow_call`) workflow's
default `actions/checkout` checks out the **caller's** repository, not this
one -- `github.repository` resolves to the caller inside a called
workflow's job. Added a required `projectbuddy-ref` input so callers pass
back the exact same commit SHA they pinned in their own `uses:@<ref>` line,
and the checkout step now explicitly targets
`repository: VITechnologies/projectbuddy` at that ref. Confirmed live
2026-11-04: v0.1.0's reusable-workflow path failed with "term
'./scripts/Test-ToolchainPresence.ps1' is not recognized" because it had
checked out the caller's repository instead.

## v0.1.0

Initial public release: establishes the reusable-workflow/script consumption
mechanism (see [README.md](README.md)) with a minimal, side-effect-free
toolchain smoke-test workflow and script as a proof of the mechanism.

