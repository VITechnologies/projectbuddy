# ProjectBuddy

- Status: Active
- Last substantive update: 2026-11-07
- Owner: svelderrainruiz

A public, project-agnostic engine for provisioning self-hosted build/test
infrastructure and running native SBOM reporting pipelines, maintained by
[VI Technologies](https://github.com/VITechnologies).

Anyone forking this repository can use it for their own projects by
supplying their own `.toml`/`.json` project configuration — this repository
ships with zero pre-committed project configs, VI
Technologies-specific identifiers, or private infrastructure state, except
in a small number of scripts explicitly disclosed as narrow-purpose,
consumer-vendored reference templates for one specific known consumer
(see [`#68`](https://github.com/VITechnologies/projectbuddy/issues/68)
for the full history and remediation evidence).
Private consumers (such as `VITechnologies/SandboxSBOMReporting`) supply
their own real project configuration and infrastructure state separately,
and consume this repository's reusable workflows and scripts as a
versioned, pinned dependency — never the other way around. No credentials
or private configuration ever flow into this repository.

## Standards compliance

Assessed against `svelderrainruiz/repo-standards-review`'s `release-gate`
profile, **version `v0.2.23`** (commit `ba72187`). See
[`docs/architecture/standards-compliance.md`](docs/architecture/standards-compliance.md)
for the full gate status and the living re-certification policy,
[`docs/architecture/adr/ADR-0001-adopt-gitflow-for-release-governance.md`](docs/architecture/adr/ADR-0001-adopt-gitflow-for-release-governance.md)
for the branch-governance decision this assessment drove, and
[`CONTRIBUTING.md`](CONTRIBUTING.md) for the branch model and the policy
on proactively filing issues for spec drift and future-agent-success
risks.

## Consumption model

- **Reusable GitHub Actions workflows** are called by downstream repositories
  via `uses: VITechnologies/projectbuddy/.github/workflows/<name>.yml@<commit-sha> # <tag>`
  (pinned to the exact release commit SHA, with the tag as a trailing
  comment for readability -- see `toolchain-smoke-test.yml`'s own header
  comment for the canonical example, the only workflow here that is
  actually `workflow_call`-consumable this way), matching the pattern
  already established for LabVIEW plugin module releases in the
  `labview-benchmark-actor-plugins` org.
- **`workflow_dispatch`-only workflows** (`add-project.yml`,
  `provision-windows-labview-vm.yml`, `provision-ubuntu-vm.yml`,
  `vm-fleet-status.yml`) cannot be pinned via `uses:@sha` -- a consumer
  vendors its own copy of these instead (see `Add-Project.ps1`'s own
  `$WorkspaceRoot` doc comment for the rationale, resolved as intentional
  in `#48`), accepting that vendored copies drift from this engine's own
  until a consumer deliberately re-syncs them.
- **Directly-invoked scripts** (not wrapped in a GitHub Actions workflow) are
  consumed via a second `actions/checkout` step pinned to the exact release
  commit SHA (not just the tag), inside any downstream workflow that needs
  them — the same trust model GitHub's own `uses: owner/repo@<sha>` syntax
  already applies to every action pinned throughout this engine's callers.
  This repository does not use git submodules for this purpose.
- **Upgrade path**: downstream repositories bump their pinned tag/SHA
  deliberately, as a reviewed code change (a version-bump commit), never
  automatically and never by tracking a floating branch or `latest`.

See [CHANGELOG.md](CHANGELOG.md) for released versions.

## What's in this engine

### VM provisioning (`vm/windows-labview/`, `vm/ubuntu-labview/`)

Dry-run-first VirtualBox builders that create self-hosted build/test VMs
(Windows 11 + LabVIEW/VIPM, or Ubuntu 24.04 + LabVIEW/VIPM), register them
as GitHub Actions self-hosted runners, and track them in a fork-local
`runner-inventory.json` (copy [`vm/windows-labview/runner-inventory.example.json`](vm/windows-labview/runner-inventory.example.json)
to get started — it is the committed schema/template; your own real
instance is fork-local and never committed here).

The VM identity values that actually name a fork's own infrastructure
(`VM_NAME`, `BASEFOLDER`, `GUEST_FULLNAME`, `GUEST_HOSTNAME`) are mandatory
inputs with no built-in default — these scripts never assume any fork's
own naming. (A few non-identity parameters, like the ISO path, guest
username, and WinRM port, do have generic, non-fork-specific defaults for
convenience; see each script's own `param()`/argument-parsing block for
the authoritative list.) `vm/windows-labview/` additionally includes an NSIS/VNC
WinRM-bootstrap mechanism (`winrm-setup-installer.nsi`,
`build-nsis-bootstrap-iso.sh`, `vnc-bootstrap.py`) that configures WinRM on
a freshly-installed Windows guest without depending on `FirstLogonCommands`
surviving an in-guest restart — see the `.nsi` file's own header comment
for the full rationale.

`.github/workflows/provision-windows-labview-vm.yml` and
`provision-ubuntu-vm.yml` are the corresponding provisioning entry points,
each taking every real identity value as an explicit `workflow_dispatch`
input (some carry a generic, clearly fictional placeholder default; none
default to any specific fork's real infrastructure).
`vm-fleet-status.yml` is a separate, read-only verification workflow with
**zero** `workflow_dispatch` inputs — it re-derives ground truth from a
fork's own committed `vm/windows-labview/runner-inventory.json` (never
committed in this public engine repo itself, so its 30-minute schedule
trigger here finds no inventory and cleanly no-ops; a consuming fork with
a real, committed inventory gets full per-VM verification). See
[`docs/vm-provisioning/procedure.md`](docs/vm-provisioning/procedure.md)
for the full phase-by-phase procedure (both VM families), including the
deliberate human-only LabVIEW activation boundary (VIPM activation on the
Ubuntu-LabVIEW family is a deliberate, narrower automated exception) and
the active fleet-status verification design.
[`docs/vm-provisioning/ubuntu-lane-box.md`](docs/vm-provisioning/ubuntu-lane-box.md)
covers the LabVIEW-free Ubuntu lane box (`vm/ubuntu-lane/`,
`scripts/Invoke-LaneBox.py`).

### Native SBOM reporting (root-level `Run-*`, `Build-Sbom.ps1`, `internal/`, `docker/`)

`Run-Pipeline.ps1` runs the native Windows LabVIEW build-and-report
pipeline directly on a self-hosted runner (there is no native Linux
equivalent in this repo -- `Run-Pipeline.sh` is a separate, unrelated
LabVIEWCLI MassCompile/VI Analyzer tool for NI's own example VIs, not a
SBOM pipeline; see `docs/sbom-pipeline/procedure.md`'s Native pipeline
section). `Run-WindowsSbom.ps1`/`Run-LinuxSbom.ps1` run the equivalent
containerized (Docker) pipelines on either platform. All three read
project configuration from either the `PROJECTS_OVERRIDE` environment
variable (the documented CI mechanism) or an explicit
`-ConfigName`/`-VipmTomlPath` local file — neither has a built-in
default pointing at any specific project; supply your own.

Dependency-Track reporting (`full` mode) requires an explicit
`DEPENDENCY_TRACK_URL` — there is no built-in default pointing at any
particular organization's server; a fork without one configured fails
closed with a clear error rather than silently trying to reach someone
else's private instance.

`docker/linux-sbom/` and `docker/windows-sbom/` are the container images'
own Dockerfiles and in-container pipeline scripts. See
[`docs/sbom-pipeline/procedure.md`](docs/sbom-pipeline/procedure.md) for
the full stage-by-stage procedure (both native and containerized paths),
including the full-mode fail-fast reporting preflight, the LVLIBP
release-dependency attestation-verification chain, and the deliberate
native-vs-container difference in whether Grype's own findings gate the
run.

### LUnit release gate (`docker/lunit-gate/`)

An ephemeral, no-VIPM, GitHub-hosted-runner container (separate from the
SBOM pipeline's own containers above) that runs a pinned commit of
`ni/labview-icon-editor`'s real LUnit test suite via native `LabVIEWCLI`,
proving this engine can execute genuine, unattended LabVIEW automated
tests without depending on persistent-VM GUI activation. Both Windows
(Phase 1) and Linux (Phase 2) are implemented and live-dispatch-proven on
both the fork and upstream `VITechnologies/projectbuddy` -- per `REQ-033`
the dual-platform gate is now **Active**. See
[`docs/lunit-gate/procedure.md`](docs/lunit-gate/procedure.md) and
`docs/architecture/adr/ADR-0003-ephemeral-container-lunit-release-gate-revised.md`
for the full procedure, the SHA-256 package-pinning provenance, and the
named-waiver policy.

### Runner/registration helpers (root-level)

`Register-WindowsRunner.ps1` and `Register-WslRunnerKeepAlive.ps1` plan or
install a self-hosted Windows Actions runner as a service (optionally under
a dedicated local account) and keep a WSL-hosted runner's distribution
running, respectively. `Add-Project.ps1` validates and dispatches a
project-registration workflow call (`add-project.yml`-equivalent) against
the schema in `pipeline-config.schema.json`.

### Human escalation (`New-HumanEscalation.ps1`, `Get-HumanEscalations.ps1`)

A first-class, project-agnostic mechanism for flagging a blocker that
automated iteration cannot resolve on its own, instead of leaving that
determination buried in an ad-hoc issue comment. `New-HumanEscalation.ps1`
creates (or, re-run with the same `-EscalationId`, idempotently updates) a
structured GitHub issue in any caller-supplied repo: context, why the agent
could not resolve it, options considered, a recommendation if one exists,
and related issue links, all under a consistent `needs-human-decision`
label (configurable via `-Label`). `Get-HumanEscalations.ps1` queries one
or more repos for open issues carrying that label and prints a single
consolidated dashboard, so a human reviewing multiple forks/repos in this
ecosystem does not have to check each one individually.

This is deliberately a plain script, not a GitHub Actions workflow: the
decision to escalate is a judgment call made during an agent's own
analysis, not a routine CI step, so it is invoked directly (second
consumption model above) at the point that judgment is made.

### Human approval gate (`scripts/Invoke-ApprovalGate.py`, `approval-gate.yml`)

Where an escalation only notifies a human, the approval gate lets a human
authorize one privileged action. An automated requester submits a request
with `scripts/Invoke-ApprovalGate.py submit`. The request becomes one run
of `approval-request.yml`, whose gated job waits in a protected GitHub
environment for a human required reviewer. After an approval, the job
re-verifies it, then runs the action class's executor with the credential
that only that environment holds. Every decision is recorded on a
tracking issue. Forks and other repositories adopt the gate by
configuration: an action-class registry, one environment per class, and a
`readiness` check that names anything missing. See
[`docs/approval-gate/procedure.md`](docs/approval-gate/procedure.md) and
[`ADR-0007`](docs/architecture/adr/ADR-0007-human-approved-automation-requests.md).
Phase 1 ships the gate with only a no-op known-answer class, so it is not
yet live-proven.

### Repo onboarding (`Enable-RepoIssueTracking.ps1`)

Many repos this engine builds for start with Issues disabled, which forces
their technical issues into whichever consumer repo happened to drive the
work instead of their own tracker. `Enable-RepoIssueTracking.ps1` enables
Issues on a given `owner/repo` (idempotent, never disables anything) and
seeds the `needs-human-decision` label so the repo is immediately ready for
both normal issues and `New-HumanEscalation.ps1` escalations.

`add-project.yml` calls this automatically, as a best-effort step, for
every GitHub-hosted repo a newly-registered project's config references
(`repoUrl`, `sourceCheckouts[].repository`,
`build.lvlibpDependencies[].repository`) -- this needs a repo-scoped
`ACTIONS_REPO_ADMIN_TOKEN` secret (`administration:write` on the target
repos; distinct from `ACTIONS_VARIABLES_TOKEN`, which is Variables-scoped
only). A missing or insufficiently-scoped token only logs a warning; it
never fails project registration itself.

Projects outside the `PROJECTS_OVERRIDE` registration mechanism (e.g. a
standalone source repo being brought into a separate cross-repo build-spec
effort) have no formal onboarding workflow yet -- run
`Enable-RepoIssueTracking.ps1` directly against such a repo as the first
step of bringing it in, until/unless that kind of effort gets its own
onboarding workflow.
