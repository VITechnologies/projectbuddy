# ADR-0004: Linux-Specific LUnit Named Waivers (`REQ-036`)

- Status: Accepted
- Date: 2026-11-07
- Deciders: svelderrainruiz (operator), informed by an independent
  3-model (Claude Opus, GPT-6-astra, Gemini-4-argon) post-implementation
  verification review of `#87`'s Phase 2 (`#94`)

## Context

`ADR-0003`'s round-2 Addendum narrowed this gate's waiver scope
explicitly: *"the only waiver permitted without a further decision is
the specific, named `Test Write Read Library Icon Data.vi` tag-mismatch
exception... Any other failing test blocks the gate; waiving a different
test requires an explicit new ADR, not a quiet SRS reinterpretation."*
It also requires each such waiver to be *"an explicitly named,
individually tracked waiver with its own follow-up issue and expiry
condition (re-evaluated at the next pin update)."*

Phase 2's implementation (merged via `#92`/`#93`) added two additional
named waivers to `invoke_lunit_tests_linux.py`'s
`ALLOWED_WAIVER_TEST_NAMES` directly, documented only via an addendum to
`ADR-0003` and an SRS table edit -- **without** the new ADR this
project's own rule requires, and without individual follow-up
issues/expiry conditions for each. An independent post-implementation
verification review (`#94`, 3 models: Claude Opus, GPT-6-astra,
Gemini-4-argon) caught this as a real process violation, consistent with
this project's standing practice of a mandatory second, explicitly
verification-framed pass after any fix/implementation round. This ADR
corrects it retroactively, by formally deciding what was already
deployed rather than leaving it as an undocumented gap.

Both waivers are genuine, confirmed platform differences, not functional
defects -- each verified by direct inspection of the exact assertion
diff in the real `lunit.xml` produced by the live-dispatch-proven CI
runs (fork `42372457595`, upstream `42372700928`):

- **`Test Window Position.vi`**: 7 of 8 window-bounds assertions fail by
  an exact, consistent 1-pixel offset across all four coordinates (e.g.
  `Expected: Left: 211` vs. `Actual: Left: 212`) -- a
  Linux-window-manager-vs-Windows rendering difference in the test's
  hardcoded expected coordinates (captured against a Windows LabVIEW
  environment). `Position when Use Size is False` passes.
- **`Test INI Settings.vi`**: both of its 2 assertions fail because they
  expect Windows-specific default font names (`"Small Fonts"`,
  `"Calibri"`) and get Linux LabVIEW's actual default,
  `"LabVIEW Application"`, instead -- a platform default-font-naming
  difference.

## Decision

Both tests are formally waived for the **Linux** LUnit gate only (not
Windows -- Windows does not exhibit these failures), each as an
individually named, tracked waiver:

| Waived test | Root-cause evidence | Follow-up issue | Expiry condition |
| --- | --- | --- | --- |
| `Test Window Position.vi` | Consistent 1-pixel window-bounds offset, all 4 coordinates, confirmed against the real `lunit.xml` | `#95` | Re-evaluated at the next `suite_commit` pin update; closes if upstream makes this test platform-tolerant |
| `Test INI Settings.vi` | Windows-only default font names (`Small Fonts`/`Calibri`) vs. Linux's real default (`LabVIEW Application`), confirmed against the real `lunit.xml` | `#96` | Re-evaluated at the next `suite_commit` pin update; closes if upstream makes this test platform-tolerant |

`REQ-036`'s complete, self-contained waiver list is therefore exactly
three named tests: these two, plus `Test Write Read Library Icon
Data.vi` (shared with `REQ-034`/Windows, already authorized by
`ADR-0003`). No other test's failure or error is permitted to pass the
Linux gate without a further new ADR, per `ADR-0003`'s existing rule.

`docs/lunit-gate/procedure.md`'s "named-waiver policy" section is
updated to reflect this -- it previously stated a single Windows-only
waiver as "the **only** test case permitted," which is now Windows-gate-
specific language; the Linux gate's own three-entry list is documented
separately.

## Consequences

- `REQ-036`'s waiver list is now backed by a formal decision record, not
  only an SRS table edit -- closing the process gap `#94`'s review
  found.
- Two new follow-up issues (`#95`, `#96`) exist, each with its own
  re-evaluation trigger (the next `suite_commit` pin bump), matching the
  existing pattern for the Windows waiver.
- Bumping the pinned `suite_commit` requires re-running the Linux gate
  and checking whether either waiver is still needed; if either test no
  longer fails, its waiver lapses and its tracking issue should be
  closed, not silently carried forward.
- Waiving any *additional* test (beyond these three) still requires a
  new ADR, unchanged from `ADR-0003`'s existing rule.

## Related

- `ADR-0003-ephemeral-container-lunit-release-gate-revised.md` (the rule
  this ADR complies with; left unedited)
- `#87` (LUnit gate implementation milestone)
- `#94` (the verification review that found this gap)
- `#95`, `#96` (per-waiver follow-up issues)
- Wiki SRS `REQ-036`
