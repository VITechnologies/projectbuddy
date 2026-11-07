# Adapting the LUnit gate for a new project

- Status: Active
- Last substantive update: 2026-11-07
- Owner: svelderrainruiz

`#98` scoped the LUnit release gate's long-planned "cookiecutter" reusable
template (per `ADR-0003`'s own stated follow-on, once both platforms
worked -- they now do, independently verified per `#94`/`#97`). The
conclusion: a full `cookiecutter`-tool investment is premature given
zero other consumers currently exist that need this exact pattern --
building and maintaining a templating tool for a speculative future
consumer violates this engine's own evidence-based, YAGNI-respecting
practice. The parameterizable surface is also genuinely small: five
touchpoints, each marked `CUSTOMIZE PER PROJECT` in the source itself.
This guide is the lightweight alternative: a concrete runbook for
adapting the gate by hand, upgradeable to a real template later if a
second real consumer appears and clarifies which patterns are actually
shared versus one-off.

## The five touchpoints

Each is marked with a `CUSTOMIZE PER PROJECT` comment in both platforms'
source (`docker/lunit-gate/invoke_lunit_tests_linux.py` and
`docker/lunit-gate/Invoke-LUnitTests.ps1`) so a `grep -rn "CUSTOMIZE PER
PROJECT"` finds every one of them:

1. **Suite owner/repo** -- the GitHub `owner/repo` of the LUnit-testable
   LabVIEW project (`get_pinned_suite_archive("ni", "labview-icon-editor", ...)`
   / `Get-PinnedSuiteArchive -Owner 'ni' -Repo 'labview-icon-editor' ...`).
2. **Project file path** -- the `.lvproj` file's path within that repo's
   archive root (`lv_icon_editor.lvproj` today).
3. **Pinned suite commit** -- already a `workflow_dispatch` input
   (`suite_commit`) on both `lunit-gate-windows.yml`/`lunit-gate-linux.yml`;
   just change its `default` to the new project's own pinned commit (see
   `docs/lunit-gate/procedure.md`'s "Suite commit pinning" for the
   pinning discipline itself, which is already fully reusable as-is).
4. **Named waiver list** -- `ALLOWED_WAIVER_TEST_NAMES` (Linux) /
   `$script:AllowedWaiverTestName` (Windows). Start empty (or with
   whatever real, confirmed platform differences the new project's own
   suite has -- see `ADR-0003`'s/`ADR-0004`'s waiver-authorization
   process, which is itself fully reusable: a new ADR is still required
   per this engine's own governance to waive anything).
5. **Expected test count** -- `EXPECTED_TEST_COUNT` (Linux) /
   `$expectedTestCount` (Windows). Set to the new project's suite's real,
   confirmed total test-case count (run the gate once locally first,
   per "Fast local iteration" below, and read the count off the real
   `lunit.xml`/`gate-result.json` -- never guess this number).

Everything else -- the digest-pinned base image pattern, SHA-256 package
staging (`Get-VerifiedLUnitPackageStaging.ps1`, fully project-agnostic:
it stages the LUnit/LUnit CLI testing framework itself, not anything
project-specific), the NSIS-installer (Windows) / plain-copy (Linux)
deployment mechanics, the no-VIPM/no-secrets manifest check, and the
JUnit-report validation logic itself -- is already fully reusable
as-is, with no project-specific assumptions baked in.

## Adaptation steps

1. Copy `docker/lunit-gate/` and `.github/workflows/lunit-gate-{windows,linux}.yml`
   into the new project's own repo (or a new subdirectory of this one,
   if the new project is itself a consumer of this engine).
2. Edit the five touchpoints above.
3. Validate locally first, per `docs/lunit-gate/procedure.md`'s own
   "Fast local iteration" sections (the Vagrant harness for Windows, the
   direct local Docker run for Linux) -- never skip straight to CI
   dispatch, matching this engine's own established discipline.
4. Dispatch a real `workflow_dispatch` run and confirm the genuine
   `gate-result.json` before considering the adaptation complete.
5. If the new project's suite turns out to need platform-specific
   waivers Linux's three or Windows's one don't already cover, follow
   `ADR-0003`'s waiver-scope rule: a new ADR, a named/tracked follow-up
   issue per waiver, and an expiry condition tied to the next pin update
   -- exactly like `ADR-0004` did for this project's own two Linux
   waivers.

## When to revisit the "full cookiecutter tool" decision

If a second real, concrete consumer of this exact pattern appears,
revisit this decision with real evidence from *two* independent
adaptations in hand (not speculation about a single, hypothetical one)
-- that comparison will reveal which of the five touchpoints above (if
any) turn out to need more sophisticated templating than a plain
find-and-replace, and which stay genuinely simple as marked.

## Related

- `ADR-0003` (`docs/architecture/adr/ADR-0003-ephemeral-container-lunit-release-gate-revised.md`)
  -- the waiver-governance rule this guide's step 5 points back to
- `ADR-0004` (`docs/architecture/adr/ADR-0004-linux-lunit-named-waivers.md`)
  -- the real precedent for adding a project-specific waiver correctly
- `docs/lunit-gate/procedure.md` -- the full existing procedure, package
  provenance, and fast-iteration harnesses this guide builds on
- `#87` (LUnit gate implementation milestone), `#98` (this scoping work)
