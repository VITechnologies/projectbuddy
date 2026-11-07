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

## Related

- `ADR-0002` (superseded by this decision; left unedited, Status updated)
- `#87` (post-release implementation milestone; re-scoped per this ADR's
  phased approach)
- `#62` (reopened; this decision does not resolve it)
- `ni/labview-icon-editor#575` (reference design; open/unmerged at the
  time of both `ADR-0002` and this ADR)
