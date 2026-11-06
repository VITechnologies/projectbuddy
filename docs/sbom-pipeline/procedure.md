# SBOM Pipeline Procedure

Satisfies `repo-standards-review`'s DOC/ARCH gaps for the native and
containerized SBOM pipelines (`VITechnologies/projectbuddy#32`, following
the same gap pattern `#29` closed for VM provisioning). Describes the
real, already-implemented behavior of `Run-Pipeline.ps1`/`.sh`,
`Build-Sbom.ps1`, `internal/Pipeline.Stages.ps1`, and
`docker/linux-sbom/`/`docker/windows-sbom/` -- this is a
procedural/operational document, not a requirements document; see the
wiki's Software Requirements Specification (`REQ-022` onward) and
Architecture Packet for the governed requirements and architecture views
this procedure implements.

Both the native and containerized pipelines build the same kind of
output (a CycloneDX 1.5 SBOM, a Grype scan, optional Dependency-Track
publishing) from the same kind of input (one project's VIPM manifest +
VCS checkout), but differ in where they run and several real behavioral
details below -- this document exists specifically so a reader
troubleshooting one does not have to assume it behaves like the other.

## Shared concepts

- **Reporting mode** (`DEPENDENCY_TRACK_MODE`, `full` by default or
  `hosted-grype`): `full` mode requires `DEPENDENCY_TRACK_API_KEY`, cannot
  disable the Critical/High security gate, and fails the *entire* run
  immediately -- before any project's checkout even starts -- if the
  configured `DEPENDENCY_TRACK_URL` is not an absolute HTTPS URL or does
  not answer a real `GET .../api/openapi.json` preflight request.
  `hosted-grype` mode skips Dependency-Track publishing/gating entirely.
- **`pipeline-run.json`**: every run writes one per-project record
  (`schemaVersion`, `project`, `pipeline`, `reportingMode`, `status`,
  `failureCategory`, per-stage `status`/`gate`). `failureCategory` is set
  from *which* stage was still `running` when a failure was thrown: a
  stage marked `gate: failed` anywhere -> `policy`; still running in the
  `dependencyTrack` stage -> `reporting`; anything else -> `build`. This
  is the exact mechanism `Test-SbomArtifact.ps1`'s 3 distinct exit codes
  (1/2/3) read, and the reason a skipped stage is never mistaken for
  missing output.
- **`vipm-exit-codes.json`**: a committed lookup table of VIPM CLI exit
  codes with the remediation hint text both the native and containerized
  pipelines print on a failed `vipm` invocation (sourced from the VIPM CLI
  docs, not invented).
- **`pipeline-config.schema.json`**: the JSON Schema every project's
  pipeline configuration is validated against before any of its stages
  run (both natively via `Test-Json` in `Run-Pipeline.ps1`, and the same
  schema file mounted into both container images).

## Native pipeline (`Run-Pipeline.ps1` on Windows, `Run-Pipeline.sh` on Linux)

Runs directly on a self-hosted runner (no container). Per project, in
order:

1. **Checkout** (`Invoke-PipelineCheckoutStage`) -- wipes any existing
   working directory (including hidden `.svn`/`.git`, since a stale one
   would silently turn a fresh checkout into an update of old content),
   then checks out the configured VCS (git or SVN, with SVN sparse-path
   support).
2. **Source checkout** (`Invoke-PipelineSourceCheckoutStage`, optional,
   `sourceCheckouts` config) -- stages additional Git repositories inside
   the project's own checkout. Hardened against path-traversal: rejects a
   `checkoutPath` that is rooted, contains `..` segments, traverses a
   symlink/reparse-point ancestor, or already exists at the destination.
3. **LVLIBP release dependency** (`Invoke-LvlibpReleaseDependencyStage`,
   optional, `build.lvlibpDependencies` config) -- fetches a pre-built
   `.lvlibp` from a GitHub Release, but only after a full supply-chain
   verification chain, not a bare download: the release must be published
   and *immutable* (not a draft); `gh release verify` must return a valid
   in-toto `https://in-toto.io/attestation/release/v0.2` attestation whose
   subject matches the requested repository/tag/purl; the tag reference
   must resolve (following up to 10 levels of annotated tags) to a real
   commit SHA; the downloaded `release-manifest.json` must cross-match
   that resolved commit, the requested version, and the release tag; each
   asset (including the SBOM, required to be CycloneDX 1.5) must have
   exactly one matching, attested, SHA-256-verified entry, with duplicate
   filenames rejected outright. Any mismatch at any step throws rather
   than falling back to an unverified download.
4. **Manifest** (`Invoke-PipelineManifestStage`) -- runs `vipm init` at
   the checkout root, then overwrites the freshly-initialized
   `vipm.toml` with the project's configured manifest content verbatim
   (exact UTF-8, no BOM).
5. **SBOM** (`Invoke-PipelineSbomStage`) -- runs `vipm sbom` to produce a
   CycloneDX 1.5 SBOM. `ALLOW_PACKAGE_DRIFT=1` (the default) downgrades a
   drifted-package mismatch to a warning, recording the actually-installed
   versions in the SBOM instead of failing.
6. **Scan** (`Invoke-PipelineScanStage`) -- runs Grype against the
   enriched SBOM. **The native pipeline does not gate on Grype's own
   findings** -- it only reports them (`gate: not-gated`); Dependency-Track's
   own metrics are the pipeline's real security gate (step 8).
7. **Policy gate** (`Test-PipelinePolicyGate`, `full` mode only) --
   requires Dependency-Track's returned metrics to be bound to the exact
   BOM just uploaded (`lastOccurrence` is checked against a required
   occurrence marker, not just "some metrics exist"), and fails the run
   if Critical or High vulnerability counts are nonzero.
8. **Publish** (`Publish-DependencyTrack`) -- uploads the SBOM, polls for
   and patches project metadata, exports server-side findings (best
   effort -- an export failure is recorded on the run record rather than
   failing the whole stage), then evaluates the policy gate above.

## Containerized pipeline (`docker/linux-sbom/`, `docker/windows-sbom/`)

Mirrors the native pipeline's SBOM-producing stages (checkout -> deploy
manifest -> `vipm init`/`lock`/`sbom` -> enrich -> scan -> publish) inside
a container built from NI's own official LabVIEW image (Linux or
Windows), for environments without a persistent self-hosted LabVIEW
runner. Both `linux_sbom_pipeline.py` and `windows_sbom_pipeline.ps1`
carry their own complete environment-variable reference in their own
module docstring -- summarized here, not duplicated verbatim:

- **`SBOM_SOURCE`**: `vipm` (default, requires VIPM Pro and runs the full
  `init`/`lock`/`sbom` sequence) or `manifest` (builds only the base SBOM
  from the deployed `vipm.toml`, no VIPM invocation, no `vipm.lock`
  merge) -- useful when the container doesn't have a licensed VIPM Pro
  available.
- **VIPM Pro activation** (both `docker/linux-sbom/entrypoint.sh` and
  `docker/windows-sbom/entrypoint.ps1`): on every container start where
  `SBOM_SOURCE` is not `manifest`, the entrypoint requires
  `VIPM_SERIAL_NUMBER`/`VIPM_FULL_NAME`/`VIPM_EMAIL` to be set and
  activates VIPM Pro via `vipm activate --direct-vipm-io`, chosen
  specifically because VIPM Desktop's first-run licensing dialogs cannot
  be answered inside a container. The serial number is redacted from any
  activation error output before it reaches the container's stdout/
  stderr, and all 3 environment variables are unset immediately after
  activation completes (or is skipped), before the pipeline itself runs
  -- so they are never visible to the SBOM pipeline process or anything
  it shells out to. `SBOM_SOURCE=manifest` is the way to run the
  container without supplying real VIPM Pro credentials at all; this is
  a deliberate, repeated pattern in this engine (the Ubuntu-LabVIEW VM
  family's `complete-activation` phase performs the equivalent
  non-interactive VIPM activation the same way -- see `REQ-032` in the
  wiki's Software Requirements Specification).
- **Platform-specific dependency-closure enrichment**: the Linux image
  replaces the native pipeline's Windows-only `nipkg` deep scan with a
  recursive `dpkg` scan of the NI Debian packages installed in the
  container (`INCLUDE_OS_DEPENDENCIES` controls whether non-NI OS
  packages are kept); the Windows image instead adds the installed NI
  dependency closure from `nipkg info-installed` (read-only -- it never
  resolves available feed packages or changes the installation).
- **`GRYPE_FAIL_ON`**: unlike the native pipeline (which never gates on
  Grype, only reports it), **both containerized pipelines apply Grype
  itself as a real severity-based gate** (`negligible`/`low`/`medium`/
  `high` (default)/`critical`, or `none` to report-only) -- a genuine
  behavioral difference from the native path, not just an implementation
  detail, worth knowing before assuming container and native runs will
  fail/pass identically on the same vulnerable dependency.
- **`/engine` (Linux) / `C:\engine` (Windows)**: an optional second mount
  of this generic engine's own root (`Build-Sbom.ps1`,
  `pipeline-config.schema.json`, `vipm-exit-codes.json`), falling back to
  the workspace mount for a standalone container run with no separate
  engine mount -- added in `v0.3.6` so a consumer no longer needs to keep
  its own local copy of these engine-owned files once it stops vendoring
  them.
- `docker/windows-sbom/Build-LvlibpCandidate.ps1` is a distinct,
  narrower script (not part of the main SBOM pipeline flow): it builds
  one specific hosted `.lvlibp` candidate from a `dependencies` project
  mounted at a fixed `C:\workspace\dependencies` path, for the
  `dependencies` repo's own `build-lvlibp-candidate.yml` CI workflow.

## Related schemas and lookups

- `pipeline-config.schema.json` -- validates every project's pipeline
  configuration before any stage runs.
- `vipm-exit-codes.json` -- VIPM CLI exit-code name/remediation-hint
  lookup, shared verbatim by the native pipeline and both container
  pipelines.
