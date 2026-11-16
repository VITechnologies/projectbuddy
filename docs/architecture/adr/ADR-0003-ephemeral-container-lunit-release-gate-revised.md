# ADR-0003: Ephemeral-Container LUnit Release Gate (Revised, Supersedes ADR-0002)

- Status: Accepted
- Date: 2026-11-07
- Deciders: svelderrainruiz (operator), informed by a 3-model
  (Claude Opus, GPT-6-astra, Gemini-4-argon) independent pre-implementation
  consensus review of `ADR-0002`

## Context

- `ADR-0002` proposed adopting ephemeral-container LUnit test execution
  (modeled on `ni/labview-icon-editor#575`) as a release gate, in response
  to `#62`'s live-dispatch-proof work hitting three layered VM-GUI
  infrastructure failures while trying to hand off to a human operator
  for interactive LabVIEW/VIPM activation.
- Per operator direction, before any implementation began, `ADR-0002`
  and its draft `REQ-033`/`REQ-034`/`REQ-035` were put through a
  dedicated pre-implementation strategy-consensus review: three
  independent models (Claude Opus, GPT-6-astra, Gemini-4-argon), each
  given the same real evidence (the ADR text, the draft requirements,
  `ni/labview-icon-editor#575`'s actual Dockerfile/workflow/script
  content, this engine's own existing containerized-pipeline Dockerfiles,
  and the quoted ISO/IEC/IEEE 29148:2018 §5.2.5 requirement-quality
  characteristics), independently reviewed the strategy and requirements
  quality, with no visibility into each other's output.
- **All three independently reached the same verdict: proceed with the
  general direction, but not as drafted** -- and converged, independently,
  on several of the same specific defects:
  1. `REQ-034`'s Rationale called `ni/labview-icon-editor#575` a
     "verified-working mechanism." All three models flagged this as
     factually incorrect: the PR is open and unmerged, its own author
     disclosed "End-to-end CI confirmation of the latest registry-free
     workflow remains pending," and one test
     (`Test Write Read Library Icon Data.vi`) already has its failure
     gate downgraded to a warning, not a clean pass.
  2. `ADR-0002`'s own Consequences > Positive section ("no license
     secrets beyond what's already required for the existing
     containerized SBOM pipeline's VIPM activation") directly
     contradicted `REQ-034`/`REQ-035`'s "no VIPM-related secret or
     credential present anywhere in the container's environment" --
     all three models caught this same internal contradiction
     independently.
  3. The choice of CI runner class (GitHub-hosted, as `#575` uses, vs.
     this engine's own self-hosted fleet) was never decided -- a real
     gap, since this engine's existing containerized SBOM pipeline runs
     on self-hosted runners, a materially different environment.
  4. `REQ-034` embedded implementation detail (exact CLI flags, the port
     number, specific package versions/hashes) directly in requirement
     text, which two of the three models flagged as violating 29148
     §5.2.5's Appropriate/Singular characteristics (design-level detail
     belongs in Rationale/design docs, not the normative "shall"
     statement).
  5. `REQ-035` (Linux) was not self-contained -- it normatively depended
     on `REQ-034`'s text ("shall mirror...") rather than stating its own
     capability, and its Feasible/Correct characteristics were
     unestablished: no upstream reference exists for Linux, and nothing
     in the evidence confirms the LUnit/LUnit CLI VI packages, or
     `LabVIEWCLI`'s `LUnit` operation, actually work against
     `nationalinstruments/labview:latest-linux` (one model specifically
     flagged the open question of whether Linux LabVIEW needs a virtual
     framebuffer/`Xvfb` even when driven headlessly via CLI).
  6. Two models flagged that closing `#62` risked conflating two
     distinct questions: "can this engine prove real LabVIEW test
     execution" (which this new gate addresses) and "can
     `provision-ubuntu-vm.yml`'s `complete-activation`/`package-build`
     phases actually be live-dispatched successfully" (`#62`'s original,
     literal ask, which this gate does not answer one way or the other).
  7. Package integrity checks in the upstream reference use MD5; the
     container images are referenced as `latest-*` floating tags, not
     pinned by digest.

## Decision

- **Supersede `ADR-0002`** with this ADR. `ADR-0002`'s original text is
  left byte-identical and unedited (its Status header is updated to
  "Superseded by `ADR-0003`"), per this project's Decision Record
  convention (a new `ADR-NNNN` file for a revised decision, never an
  in-place rewrite of an already-Accepted one) -- `ADR-0002`'s record of
  what was first proposed, and why the consensus review changed it,
  stays intact and auditable.
- **The core direction from `ADR-0002` is retained**: ephemeral-container
  LUnit test execution, requiring no persistent VIPM/LabVIEW activation,
  as the release-gating proof that this engine can run real LabVIEW test
  code -- all three models agreed this direction is sound. What changes
  is how it is specified and sequenced:
- **Phased, not a single-step mandatory gate**:
  - **Phase 1 (Windows spike)**: get a real, green `workflow_dispatch`
    run of the Windows LUnit container mechanism in *this engine's own*
    CI -- not merely citing `ni/labview-icon-editor#575`'s unmerged,
    not-yet-green workflow as proof. `#575` is a reference design to
    port and adapt, pinned to its commit SHA at the time of porting, not
    evidence this engine's own implementation already works.
  - **Phase 2 (Linux feasibility spike)**: before Linux becomes a
    release-blocking requirement, prove -- via a real, disposable spike,
    not assumption -- that the LUnit/LUnit CLI VI packages and
    `LabVIEWCLI`'s `LUnit` operation actually function against this
    engine's pinned `nationalinstruments/labview:latest-linux` image,
    including resolving any headless-display (`Xvfb`/`DISPLAY`)
    requirement. If this spike fails or reveals the packages are not
    platform-portable, amend this ADR with a named fallback (e.g.,
    Windows-only gate plus a tracked follow-up issue) rather than
    silently dropping the Linux requirement.
  - **The dual-platform release gate (`REQ-033`) only takes effect once
    both phases succeed and are live-dispatch-proven** -- not on
    `#87`'s mere existence as a tracked issue.
- **Runner class: GitHub-hosted** (matching `ni/labview-icon-editor#575`'s
  own `windows-2022`/equivalent Linux runner choice), not this engine's
  self-hosted fleet. Rationale: this engine's own `ADR-0002` argued that
  long-lived, persistent infrastructure accumulates exactly the kind of
  operational risk this session found (`VBoxSVC`'s 5-day CPU runaway);
  a release gate should not reintroduce that risk on self-hosted
  infrastructure when a GitHub-hosted option is directly available and
  already proven upstream.
- **No VIPM activation, no VIPM-related secrets, on either platform, with
  no exception** -- removing `ADR-0002`'s contradictory Consequences
  bullet entirely. This gate's SBOM-pipeline sibling's own VIPM
  activation (`vipm activate --direct-vipm-io`) is a separate execution
  path with its own separate secrets; this gate shares neither.
- **Suite identity and failure semantics, defined explicitly**: the gate
  runs `ni/labview-icon-editor`'s LUnit suite pinned to a specific,
  named commit SHA (chosen at `#87` implementation time, updated
  deliberately like any other pin per this engine's existing upgrade-path
  convention). The upstream-known `Test Write Read Library Icon Data.vi`
  tag-mismatch exception may be carried forward **only** as an explicitly
  named, individually tracked waiver with its own follow-up issue and
  expiry condition (re-evaluated at the next pin update) -- not silently
  absorbed into an unqualified "zero failures" claim.
- **Integrity and pinning**: re-verify deployed LUnit/LUnit CLI package
  files via SHA-256 (not MD5, regardless of what the upstream reference
  uses), and pin the base LabVIEW container images by digest, not a
  floating `latest-*` tag, for this gate specifically.
- **`#62` reopened**: superseding `ADR-0002` does not, and never did,
  answer `#62`'s original literal question (can
  `provision-ubuntu-vm.yml`'s `complete-activation`/`package-build`
  phases be live-dispatch-proven). That remains open, separately tracked,
  and lower-priority than this gate -- not closed by this decision.
- **Requirements re-specified**: `REQ-033`/`REQ-034`/`REQ-035` are
  rewritten (still `Planned`, not yet implemented) to remove embedded
  implementation detail from requirement text, make `REQ-035`
  self-contained, name/pin the test suite and its waiver policy
  explicitly, and state the runner-class decision -- see the SRS.

## Rationale

- All three independently-run reviews agreeing on the same specific
  defects, despite no shared context between them, is strong evidence
  these are real issues and not one model's idiosyncratic take -- the
  established practice this session has followed throughout (every
  verification pass finds real defects) held again, this time applied
  *before* implementation rather than after.
- Phasing the Windows and Linux work, and gating the mandatory dual-
  platform requirement on both spikes actually succeeding, avoids
  committing to a release-blocking requirement whose feasibility (Linux
  specifically) is currently an assumption, not a demonstrated fact --
  directly addressing the Feasible/Correct gaps all three reviews raised.
- Citing an unmerged, not-yet-green upstream PR as "verified-working" was
  simply inaccurate; removing that claim and instead treating `#575` as
  a reference design to adapt and independently prove is both more
  honest and lower-risk.
- GitHub-hosted runners avoid reintroducing the exact class of long-lived
  self-hosted-infrastructure risk this session's own `VBoxSVC` finding
  demonstrated, and match the only reference implementation available.
- Reopening `#62` rather than leaving it closed avoids the real risk two
  reviewers flagged: mistaking "this engine can run some LabVIEW test
  code in a container" for "the Ubuntu VM family's remaining provisioning
  phases are proven" -- two genuinely different claims that must not be
  conflated in this repo's issue tracker or its spec.

## Consequences

- Positive:
  - A release gate whose feasibility is proven before it becomes
    mandatory, rather than assumed.
  - Removes a factual inaccuracy and an internal self-contradiction from
    this engine's own architecture record before either could mislead a
    future reader or implementer.
  - `#62`'s original, narrower technical question stays honestly tracked
    as still-open, rather than quietly subsumed by a different
    capability.
  - A documented, disclosed revision process (this ADR superseding
    `ADR-0002` openly, with the specific review findings recorded) is
    itself evidence of the kind of rigor this repo's standards-compliance
    posture claims to have.
- Negative:
  - Slower path to a mandatory gate: two spike phases must both succeed
    before `REQ-033` takes effect, rather than the gate being usable as
    soon as `#87`'s code is written.
  - If the Linux feasibility spike fails, this ADR requires a further
    amendment (a named fallback) rather than allowing silent scope
    reduction -- real, but deliberately accepted, process overhead.
  - Carrying forward the upstream `Test Write Read Library Icon Data.vi`
    waiver (if still unresolved upstream at `#87`'s implementation time)
    means this gate's "zero failures" is not absolute on day one --
    disclosed explicitly here rather than hidden in an unqualified claim.

## Addendum (2026-11-07, round-2 verification review)

Per `docs/information-item-map.md`'s Decision Record convention (append
new content, never rewrite accepted Decision/Consequences text), this
section corrects defects a second, independent 3-model review (Claude
Opus, GPT-6-astra, Gemini-4-argon) found in this ADR's own body above --
the original text is left unedited; these clarifications govern going
forward.

- **"Already proven upstream" (Decision, Runner class bullet) is itself
  a residual instance of this ADR's own Defect 1** (citing
  `ni/labview-icon-editor#575` as proven when it is open/unmerged with
  CI explicitly disclosed as pending) -- all three models independently
  caught this. Corrected reading: GitHub-hosted runners are chosen
  because they are `#575`'s own reference choice and avoid this engine's
  self-hosted-infrastructure risk, **not** because `#575`'s mechanism is
  proven to work. Phase 1's entire purpose is establishing that
  independently.
- **Waiver scope, narrowed**: `REQ-034`/`REQ-036`'s Fit Criterion
  wording ("except any individually-named, separately-tracked waiver")
  read more broadly than this ADR's own intent. Corrected: the *only*
  waiver permitted without a further decision is the specific, named
  `Test Write Read Library Icon Data.vi` tag-mismatch exception, if
  still unresolved upstream at implementation time, re-evaluated (and
  lapsing if not re-approved) at every pin update. Any other failing
  test blocks the gate; waiving a different test requires an explicit
  new ADR, not a quiet SRS reinterpretation.
- **"Pinned `nationalinstruments/labview:latest-linux`" (Phase 2) should
  read "pinned-by-digest"**, consistent with this ADR's own Integrity
  and pinning bullet -- `latest-linux` is a floating tag, not a pin.
- **"Amend this ADR" (Phase 2 fallback language) means author a new
  `ADR-NNNN`** that supersedes this one, consistent with this project's
  own Decision Record convention stated earlier in this same ADR -- not
  an in-place rewrite, regardless of how that language reads literally.
- **Two distinct commits, named explicitly**: `REQ-033`'s "pinned-commit
  LUnit JUnit report" and `REQ-034`/`REQ-036`'s "pinned-commit
  `ni/labview-icon-editor` LUnit suite" refer to two different
  repositories' commits -- this engine's own release-candidate commit
  (which the workflow run must be triggered against) and the external
  `ni/labview-icon-editor` suite's pinned SHA (which the suite content
  itself is pinned to). Both must be recorded with every gate result;
  neither substitutes for the other.
- See the wiki SRS's `REQ-033`-`REQ-036` for the corresponding corrected
  requirement text (self-contained Linux requirements, the narrowed
  waiver, explicit commit disambiguation) -- `#87`'s Phase 1 (the
  Windows spike) may begin immediately; these corrections govern what
  counts as a qualifying Phase 1 pass, not whether the work can start.

## Addendum (2026-11-07, Phase 2 feasibility spike -- REQ-035 outcome)

Phase 1 (Windows) completed and live-dispatch-proven (`#91`). Phase 2's
feasibility spike (`REQ-035`) then ran, confirmed via direct local Docker
testing on this engine's own host (this image's platform -- Linux -- runs
natively there, unlike Windows containers, making this dramatically
faster to validate than Phase 1's own Vagrant-based iteration) before
ever touching CI, then confirmed by real `workflow_dispatch` runs on both
the fork (`lunit-gate-linux.yml`, run
[42372457595](https://github.com/svelderrainruiz/projectbuddy/actions/runs/42372457595))
and, after merge (`#92`), upstream `VITechnologies/projectbuddy` itself
(run
[42372700928](https://github.com/VITechnologies/projectbuddy/actions/runs/42372700928)):
all three runs (local, fork, upstream) are an exact match -- `PASS`, 10
total tests, the same 3 named waivers, `vipmPresence: false`.

- **Outcome: (a), feasible.** `LabVIEWCLI`'s `LUnit` operation runs
  unattended against this engine's pinned `nationalinstruments/labview`
  Linux image using the same platform-independent LUnit/LUnit CLI VI
  package files as Windows, deployed by a plain file copy (no NSIS
  installer needed -- see below).
- **No `Xvfb`/virtual framebuffer is required.** LabVIEW's VI Server
  becomes ready and `LabVIEWCLI` completes a full run with no `DISPLAY`
  set at all -- the headless-display question `ADR-0003`'s original
  Decision raised as an open risk is resolved: it is not a real
  obstacle for this operation.
- **No NSIS-equivalent installer needed for Linux.** The two bugs that
  drove the Windows gate's NSIS migration (a Windows `MAX_PATH`
  violation, an "access denied" failure moving a directory across a
  Windows-container SMB-backed bind mount) are both Windows-container-
  specific; Linux has neither limitation. Package deployment is a plain
  `COPY` in `linux.Dockerfile`.
- **Two additional, Linux-specific named test failures** (beyond the
  Windows-shared `Test Write Read Library Icon Data.vi` tag-mismatch),
  confirmed via direct inspection of each failure's exact assertion
  diff, not assumption:
  - `Test Window Position.vi`: every window-bounds assertion fails by
    an exact, consistent 1-pixel offset in all four coordinates (e.g.
    expected `Left: 211` vs. actual `Left: 212`) -- a
    Linux-window-manager-vs-Windows rendering difference in the test's
    hardcoded expected values, not a functional defect in this
    engine's deployment.
  - `Test INI Settings.vi`: expects the Windows-specific default font
    name `"Small Fonts"`; Linux LabVIEW's actual default is
    `"LabVIEW Application"` -- a platform default-font-naming
    difference, not a functional defect.
  - Both are added as individually named waivers (per this ADR's own
    waiver-scope correction above: named, reasoned, not a blanket
    allowance) in `invoke_lunit_tests_linux.py`'s
    `ALLOWED_WAIVER_TEST_NAMES` and the wiki SRS's `REQ-036`. Waiving a
    *different* test still requires a new superseding ADR.
- `REQ-036` (Linux execution) is now implemented and live-dispatch-
  proven against the same pinned suite commit as Windows, with its own
  complete, self-contained three-waiver list (not a "same as `REQ-034`"
  cross-reference) -- resolving the round-2 review's self-containment
  finding concretely, with real evidence, rather than by wording alone.

## Addendum (2026-11-07, `#94` independent verification review + fix round)

Per this engine's standing practice of a mandatory second,
verification-framed pass after any implementation round: an independent
3-model (Claude Opus, GPT-6-astra, Gemini-4-argon) review of Phase 2's
actual code found real defects (`#94`, fixed via `#97`) -- the prior
Addendum's "no `Xvfb`/`DISPLAY` at all" claim above is corrected by this
Addendum, not rewritten in place: a direct process-tree inspection shows
LabVIEW spawns its own internal `Xvfb :99` virtual framebuffer when none
is externally provided. The accurate claim is that this engine's own
deployment needs no *externally-provided* Xvfb/DISPLAY setup -- not that
no virtual framebuffer exists anywhere in the process tree.

Other real defects found and fixed: a Linux VIPM-detection check that
looked at the wrong (Windows-shaped, non-existent-on-Linux) paths; no
report-completeness check (a truncated or skipped-test run could
previously pass); `gate-result.json` missing this engine's own
release-candidate commit, despite this ADR's own Decision requiring both
commits recorded with every gate result; `suite_commit` accepted
unvalidated as a mutable ref; an inaccurate `installMethod` manifest
value on Linux; and the two Linux waivers added without the new ADR this
ADR's own round-2 Addendum requires (closed by `ADR-0004`).

Re-verified after the fix round with fresh, real `workflow_dispatch`
runs on upstream `VITechnologies/projectbuddy` for both platforms:
Linux (run
[42375127600](https://github.com/VITechnologies/projectbuddy/actions/runs/42375127600))
and Windows (run
[42375129680](https://github.com/VITechnologies/projectbuddy/actions/runs/42375129680))
-- both `PASS`, both now recording a matching `engineCommit`, with the
same test counts and waiver lists as before the fix round, confirming
the fixes corrected the identified gaps without changing the underlying,
already-proven gate behavior.

## Related

- `ADR-0002` (superseded by this decision; left unedited, Status updated)
- `ADR-0004` (formally authorizes the two Linux-specific waivers this
  Addendum's fix round found missing a required new ADR for)
- `#87` (post-release implementation milestone; re-scoped per this ADR's
  phased approach)
- `#62` (reopened; this decision does not resolve it)
- `#94` (independent verification review), `#95`/`#96` (per-waiver
  follow-up issues), `#97` (the fix round this Addendum documents)
- `ni/labview-icon-editor#575` (reference design; open/unmerged at the
  time of both `ADR-0002` and this ADR)
