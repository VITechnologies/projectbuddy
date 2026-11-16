# Changelog

All notable changes to this project are documented here. Versions are
published as immutable GitHub releases (tag + commit SHA); see each
release's notes for the exact pinned commit.

## Unreleased

- `projectbuddy-wiki` implements `rsr/ecosystem-wiki-standard@2` (#329).
  - A wiki may declare its governed scopes, directories, ID prefixes and
    sections in a root `.wiki-profile.json`, so members such as lbabus
    (`LBA-REQ`, `T`) can conform. Without one, projectbuddy's profile applies.
  - The new whole-wiki findings `page-not-indexed` and `page-over-budget`
    (#325) report what `Assemble-WikiSpec --strict` refuses.
  - Realistic row estimates on the projection functions stop PostgreSQL from
    JIT-compiling whole-wiki queries. The projection report took 1.7 s and now
    takes 0.14 s.

## v0.4.0

- Adopt the repo-standards-review consumer kit at RSR v0.5.0 (#277), and
  re-certify the compliance stamp against v0.5.0.
  - Executable scripts under `vm`, `docker`, `scripts` and
    `.github/scripts`, and `Run-Pipeline.sh`, check out with LF on every
    platform. The CRLF Windows unattended template stays byte-exact.
  - The kit's check runs in CI and pins RSR to its published release
    manifest.
  - `standards-assurance-scan.yml` and
    `docs/architecture/standards-compliance.md` move together from v0.2.23 to
    v0.5.0. All 5 required gates `PASS`.

- `projectbuddy-wiki` pins the ecosystem wiki standard it implements:
  `rsr/ecosystem-wiki-standard@1` from repo-standards-review `v0.5.0` (#320).
  - `project` reports the pin.
  - A test compares the installed projection functions with the pinned
    catalogues, each one and in order, and fails when they differ.

- `projectbuddy-wiki project` also projects the Architecture Packet (#315).
  - Migration `0004` gives `Architecture-Packet.md` and `architecture/*.md`
    one canonical layout, `# title`, a blank line, then the body, and parses
    hub index blocks into their children.
  - Pages render back byte for byte. Problems are listed apart and never keep
    the wiki out of the registry.

- `projectbuddy-wiki export-registry` exports the wiki's governed pages to the
  RSR registry (#309, slice 2 of #307).
  - It ingests one snapshot per scope, in the order software, system, test,
    into `registry_v1`.
  - It reads the source identity from an operator-approved record.
  - It reuses a snapshot whose content key exists. Re-exporting unchanged
    content, even from a later commit, changes nothing.
  - CI tests the export against a stub of RSR's ingest contract, because RSR
    is private. The same tests pass against RSR `v0.4.2`'s own migrations.

- `projectbuddy-wiki project` projects the wiki's requirement and test pages
  for the RSR registry (#309, slice 2 of #307).
  - Migration `0003` parses each governed page in exactly one canonical
    layout, and renders it back byte for byte.
  - It derives the `verifies`, `allocates` and `supersedes` edges from wiki
    links.
  - `wiki_v1.registry_envelope` builds the `rsr/registry-snapshot@1` envelope
    of each scope, with node texts byte-identical to RSR's.
  - Any page or link that cannot be exported is listed by `project` (exit 1),
    and refuses every envelope.
  - Exit code 1 is now named `Findings`; it covers drift and projection
    problems.
  - Migration `0002` makes `seed` linear in the number of files. Seeding
    10,001 files took 25 s, because the check that no file shares its name
    with a directory was planned as a nested loop.

- `projectbuddy-wiki` keeps a local working copy of the GitHub wiki in
  PostgreSQL (#308, slice 1 of #307).
  - `seed` records the wiki clone's committed bytes as an append-only
    snapshot in schema `wiki_v1`. Each content is stored once, and a commit
    already recorded changes nothing.
  - `render` writes a recorded state back byte for byte. It writes only into
    a missing or empty directory, or into the top of a clone at a recorded
    commit (never its `.git` or a subdirectory). It refuses symbolic links.
  - `check` proves the latest state matches the clone's `HEAD`, and exits 1 on
    drift.
  - CI runs the tests against the pinned `postgres:16-alpine` image as
    `Wiki Tool Tests`, at 100% line and branch coverage.

- `projectbuddy-diagnose` raises the answer yield (#304).
  - When the rules decide, the answer prompt lists the facts they rest on,
    or, if they rest on none, the rule itself, so the model cites exact
    pointers.
  - An empty list or object can be cited as `"[]"` or `"{}"`.
  - The hint does not change grounding. On the workstation, all six
    questions gave grounded answers with verified classes.

- `projectbuddy-ask` 0.3.0 adds the `diagnose <question-id>` verb (#298, #290
  slice 4).
  - The peer's forced command runs the deployed `projectbuddy-diagnose`, with
    its pinned policy and the deny list, from fixed paths with no shell, and
    allows 180 s.
  - It returns the grounded report, or no advice with its reason, in the
    `projectbuddy/peer-ask@1` envelope. An unknown question is refused.
  - The workstation client waits longer for `diagnose` and checks the
    answer's shape, including the report's schema and verified citations.
  - The question ID cannot start with `-` or carry a trailing newline.

- `projectbuddy-diagnose` verifies the answer's class (#299, #302). The policy
  (`projectbuddy/diagnose-policy@2`) gives each question ordered,
  machine-checkable rules.
  - Conditions use `some`, `every` or `none` over JSON-pointer matches, with
    `*` over arrays, and `eq`/`ne`/`lt`/`le`/`gt`/`ge`/`prefix`/`present`
    tests.
  - A condition is undecided when its tool was not called, failed, or was
    truncated in a way that matters.
  - When the rules decide, the answer schema allows only the decided class
    from the first attempt, so the model's attempts go to the finding and
    citations. A class check stays as a defence in depth. The report adds
    `verified: ["citations", "class"]` and the rule with its basis facts.
    When the rules cannot decide, the class stays the model's, advisory.
  - The rules reach the model as generated prose.
  - The shipped rules guard against failed reads that probes report inside
    their data: services `error`, event-errors `errors`, and vm-states
    `running: null`.
  - `projectbuddy-probes services` adds `githubRunner`, decided before
    redaction can rewrite a runner's name.

- Add `tools/peer/diagnose` (`projectbuddy-diagnose`, #295, #290 slice 3): the
  bounded P4 agent loop. A local model answers one question from a closed
  list by calling only the read-only `projectbuddy-probes` tools.
  - Bounds: rounds, calls per round and in total, and an evidence byte budget.
    Every request passes the truncation guard.
  - The answer is asked for separately, in a JSON schema. It is redacted
    again and grounded: every citation's JSON pointer must resolve to exactly
    the cited value in a tool result the model was given. A rejected answer
    may be repaired once; otherwise there is no advice (exit 6).
  - The policy (`projectbuddy/diagnose-policy@1`) pins `qwen2.5:7b` by digest
    and gives each question's classes with their rules.
- `projectbuddy-probes`: `disk` reports `freePercent`, computed by the tool.
  The `ollama` description says that `identity` marks the pinned model. The
  deny-list loader is now the public `HostRedactor`, shared with diagnose.
- Coverage filters for `advise`, `bench` and `probes` use `*` wildcards.
  Coverlet ignored the hyphenated assembly names, and the gate held only
  because the Exclude lists covered everything else.

- Fix `projectbuddy-probes network-link` on Windows (#292). .NET also lists
  NDIS filter pseudo-interfaces (QoS Packet Scheduler, WFP filters) and absent
  adapters, which could fill the 16-adapter cap and push out real adapters.
  - On Windows, adapters bound to neither IPv4 nor IPv6 are now counted in
    `omitted` instead of listed.
  - Adapters with a default gateway are listed first, then those that are up,
    with absent ones last.
  - The README records what a Windows run verified, and that `vm-states` sees
    only the VMs registered to its own account.

- Add `tools/peer/probes` (`projectbuddy-probes`, #292, #290 slice 2): the
  closed list of read-only tools the P4 diagnostics agent may call on the
  peer.
  - The six tools are `network-link`, `disk`, `ollama`, `vm-states`,
    `services` and `event-errors` (`hours` 1 to 72).
  - Unknown tools and out-of-spec arguments are refused before anything runs.
  - Programs run without a shell and never from `PATH`.
  - `showvminfo` keys, services and tasks are read from allow-lists. Events
    are reduced to provider, ID and level counts.
  - Every result is redacted with the deny list and the host's own names,
    capped per list and at 16 KiB, and marked `truncated` when anything was
    cut. Results that cannot be made safe become a fixed failure envelope.
  - `run-all` adds a SHA-256 per result for the live proof. The shared Ollama
    client gains `LoadedAsync` (`/api/ps` size, VRAM and context length).
  - CI adds it to the `Peer Tool Tests` matrix at 100% coverage and publishes
    the trimmed win-x64 binary.

- Add `tools/peer/redact` (`ProjectBuddy.PeerRedact`, #290 slice 1), the
  deterministic and idempotent redaction that peer evidence passes through
  before a local model sees it and before it leaves the peer.
  - It covers secrets (token formats, `Authorization` headers including
    escaped JSON and table layouts, flags and Windows switches, command-line
    passwords for `net use`, `net user`, `schtasks`, `sqlcmd`, `bcp`, `psexec`,
    `certutil`, `docker login` and `7z`, `ConvertTo-SecureString` and
    `NetworkCredential`, prefixed and table-style assignments, connection
    strings, XML settings, SAS signatures).
  - It also covers accounts in switches and `user@address`, URLs (allow-listed
    hosts only, never a path), git remotes, e-mail addresses, directory names
    and certificate subjects, and bare host names under generic and country
    domains.
  - It also covers `reg query` secret rows, `netsh` key content,
    certificate names in LDAP, curl, OpenSSL and `certutil -dump` layouts, and
    Name/Value layouts: JSON objects at any depth (one linear scan), Format-List
    and YAML records, CSV tables, JSON argument arrays and ProcessStartInfo
    argument strings, XML elements and attributes (unattend.xml, Maven,
    `ConvertTo-Xml`, CLIXML), and PowerShell `@{…}` object strings.
  - Secret values with spaces are taken whole at a line start (after an
    optional log timestamp), after a table gap, and inside a quoted
    `KEY=value` string.
  - JSON `\uXXXX` escapes inside valid JSON string literals are canonicalized
    first, so an escaped apostrophe or umlaut cannot hide a path or a term. Raw
    text such as a Windows path is never decoded. JSON string literals with
    escapes are redacted as the text they stand for (decoded, settled and
    encoded again, at any nesting), so line-based rules work inside JSON and
    the output stays well formed; the corpus is also checked wrapped in JSON.
  - Paths are covered in every spelling: forward-slash, escaped, `file:///`,
    encoded, long, device, UNC, `//server`, Git Bash, WSL and Cygwin. A path
    runs through spaces and stops at a colon, a switch, or the next path.
  - Allow-listed paths are kept only to a fixed depth. Separators and trailing
    dots cannot dodge a limit, any `.` or `..` hides the whole path, and
    profiles are reduced to `<user>`.
  - MACs and IPs are redacted except loopback and the cable. An operator-private
    deny list (substring) plus the host's own machine and account names
    (whole word) become `<private>`, matched across any whitespace and invisible
    characters.
  - Rules repeat until the text settles (at most 32 passes, more than the
    number of rules), so the output is idempotent by construction. Evidence
    over 4,000,000 characters is refused whole unless it is already redacted,
    and every adversarial shape tested stays linear up to that cap.
  - Diagnostics stay: digests, versions, exit codes and times.
  - A synthetic corpus gates it, and CI adds it to the `Peer Tool Tests`
    matrix at 100% coverage. Its linear-time tests at the 4,000,000-character
    cap run in a separate Release step, outside the instrumented coverage run.

- Add `projectbuddy-advise pre-review` (#284 slice 2). It gives advisory
  findings on a `git diff`.
  - Each finding must name a rule from the policy rubric, which fixes its
    severity, and cite a non-blank added line. The report includes that line
    of code.
  - Large diffs are split by file, hunk and line window into requests within
    the truncation-safe budget, and coverage reports exactly what was left
    unreviewed.
  - Findings that only restate their rule's guidance are labelled `generic`.
  - Measured: F1 0.78 on the bench security corpus. On real PR diffs,
    precision is low and every false positive was `generic`, so pre-review
    stays a first look, never a substitute for review.
- Advise reports keep quotes readable: relaxed JSON escaping, since the output
  is never embedded in HTML.

- `projectbuddy-advise triage` grounding refinements from the corpus
  accuracy check (#284):
  - an exact whole log line counts from 3 characters (a bare `Killed`),
    while fragments still need 8;
  - a step not found in the log is reported as `null`, and `verified`
    lists only `errorLine`, instead of discarding a verified error line.

  On the bench corpus, all 8 items were answered with no ungrounded claim:
  7/8 error lines matched ground truth, the class was correct on 8/8, and
  1 step was dropped rather than wrong.
- Add `projectbuddy-advise triage` (`tools/peer/advise`, #284, P2 of #210). It
  turns a CI log into a few hundred bytes of local, advisory triage: the
  failing step, one error line quoted verbatim, and a class from a closed
  list.
  - Evidence shaping: timestamps and ANSI codes removed, one header per
    GitHub job/step, error-looking lines from the whole log first, then the
    newest lines within a byte budget.
  - The model (base `qwen2.5:7b`, the #217 winner) is digest-pinned in a
    committed policy.
  - Replies are accepted only after the truncation probe and the half-context
    rule. An answer that is not grounded in the evidence yields no advice
    (exit 6), never a guess.
  - On real failed runs, reports were 51–94× smaller than the raw logs.
- Extract the shared Ollama guard library `tools/peer/ollama`
  (`ProjectBuddy.PeerOllama`): the loopback-only client (no proxy, no
  redirects), the truncation probe and accept rule, model digest pins, and
  strict JSON. `projectbuddy-bench` now uses it unchanged in behavior. CI runs
  the library, bench and advise as a `Peer Tool Tests` matrix, each gated at
  100% line and branch coverage of its own assembly.
- Bench selection rule (D10): pool scores across machines and treat security
  F1 differences under 0.05 as ties, the lessons from #217.

- Add `projectbuddy-bench` (`tools/peer/bench`, #217), a .NET 10 single-file
  benchmark that scores digest-pinned Ollama candidates against #210 D10 on
  both the workstation and the peer. It covers triage extraction and
  diagnosis, security recall with clean controls, native tool calling, VRAM
  residency and latency.
  - It talks only to loopback Ollama.
  - It refuses reported prompt counts that could hide Ollama's silent
    `num_ctx/2` truncation, after a per-model probe proves the server still
    truncates that way.
  - It fails malformed model output closed.
  - CI enforces 100% line and branch coverage and a trimmed win-x64 publish.
- Add a locally tested, read-only active-capture MCP evidence consumer for
  issue #255. It validates sealed server/run/VM identity, ordered bounded
  batches, lifecycle, and explicit frame integrity without capture mutation
  or retries. Production Actor revision pinning and live qualification remain
  pending; this does not qualify a live benchmark.
- Add a local three-mode capture-time scheduling candidate with on-demand,
  periodic, and continuous modes, bounded queues and deadlines, loopback-only
  Ollama token streaming, model-digest checks, privacy transformations, and
  settled cancellation. SDK/Actor/mock-HTTP fixture composition is covered;
  model choices remain data and never authorize or execute UI actions.
- Wire an optional capture-bound one-shot dispatcher through the existing sealed
  broker and pinned MCP router. All three scheduling modes can consume one
  explicitly approved synthetic action while fixture capture remains active;
  invalid bindings, changed contracts, partial responses, and uncertain or
  contradictory acknowledgements abort without retries. Advisory mode remains
  the default, and production activation is still gated on an immutable Actor pin.
- Retain bounded numerical reading/limit observations for independent grounding
  scores, separate from action correctness and task success. Observation claims
  are not forwarded as authority or tool arguments to the sealed UI broker.
- Pin the authorized local Actor producer commit in a separate local-only
  manifest with strict base-contract/source/resource validation. The default
  production contract and live activation remain unchanged.
- Advance the opt-in pin to the published 1.5.0 standalone Windows desktop
  candidate and require explicit desktop workload admission. Native VM/live
  qualification remains blocked on the tracked Vagrant/VirtualBox import error.
- Restore the synchronous Ollama chat CPU-option guard, which was unreachable
  beneath the residency error branch; invalid execution options now fail before
  HTTP dispatch, matching the streaming transport's fail-closed policy.

## v0.3.31

Operator-authorized resolution of all 9 issues in the "Semantic Audit
Follow-ups (Rounds 1-6)" milestone, acting with full authorization to
make the judgment calls each issue was filed needing.

- **(`#68`) REQ-001 hardcoded-identifier violations**: genericized every real, accidental violation -- renamed the `sandbox-sbom-reporting/*` schema-namespace prefix to `projectbuddy/*` across 17 real producer/consumer file pairs (verified every pair still matches post-rename with a dedicated script); renamed the `/SandboxSBOMReporting/SshHostEd25519Fingerprint` guest-property path to `/ProjectBuddy/...` (4 files, plus the `guestproperty enumerate --patterns` call that searched for the old pattern); renamed `/var/lib/sandbox-sbom-reporting` to `/var/lib/projectbuddy` and the matching `~/.local/state/...` path; genericized `Register-WslRunnerKeepAlive.ps1`'s default shortcut name, `unattended-template.xml`'s 4 `<Description>` fields, `pipeline-config.schema.json`'s 2 self-referential field descriptions, and removed `Run-WindowsSbom.ps1`/`Run-LinuxSbom.ps1`'s false "VIT Super Network Streams is the default" claim. Formally scoped REQ-001's wording to exclude the 2 already-disclosed, intentional, narrow-purpose consumer-vendored exceptions (`Build-LvlibpCandidate.ps1`, `provision-ubuntu-vm.yml`'s `package-build` template) instead of treating them as bugs. Marked REQ-001 "Verified (manual)" in the wiki, no longer "NOT met."
- **(`#69`) batch-provision.sh hostname collision**: added optional `guestHostname`/`guestFullName` fields to `runner-inventory.json` (schema bumped to `@4`), wired through to each VM's `create-vm` dispatch; when omitted, derives a unique, deterministic fallback (an 11-character hash-based hostname, well under Windows's 15-character NetBIOS limit). Live-verified via a real 2-VM dry-run batch (one explicit, one derived, both distinct, full dispatch chain completing for both).
- **(`#70`) 3 sub-findings, all resolved**: (1) aligned both containerized SBOM pipelines' `ALLOW_PACKAGE_DRIFT` default from lenient (`"1"`) to strict (`"0"`), matching the native pipeline's already-documented fail-closed-by-default behavior -- **a real default-behavior change** for any consumer relying on the old lenient container default without explicitly setting the variable; (2) wired `ci-assurance.yml`'s new `toolchain-smoke-test` job to call this repo's own reusable `toolchain-smoke-test.yml` against itself (`uses: ./.github/workflows/toolchain-smoke-test.yml`, `projectbuddy-ref: ${{ github.sha }}`), making REQ-008's "validated via real CI dispatch" claim genuinely true in this repo; (3) authored real Pester fixture tests for `Test-SbomArtifact.ps1` (`scripts/tests/Test-SbomArtifact.Tests.ps1`, 6 tests covering all 3 documented failure classes plus success and a self-contradiction guard), wired into a new `pester-tests` CI job.
- **(`#76`) `Install-VerifiedNugetPackage.ps1` falsely removed**: restored the script (confirmed generic/reusable -- a SHA-512-verify-then-extract utility, nothing signing-specific) with a corrected header comment explaining the real history. Added 4 real Pester tests (success, hash-mismatch refusal, missing-archive, already-exists-destination), all passing; also manually verified end-to-end with a real `.nupkg`. Did not re-wire it into the Dockerfile (the signing toolchain it originally supported stays correctly removed). Courtesy fix in `SandboxSBOMReporting` itself: updated its stale pin-warning comment (private repo, PR #310, merged).
- **(`#77`) remaining formal-completeness + smaller REQ gaps**: added a lightweight Document Control block (Status/Last-update/Owner) to 6 docs lacking one, plus a real Notation + Glossary section to the wiki SRS -- **deliberately scoped out** a full ISO 29119-3 test-plan rewrite of `CONTRIBUTING.md`/`ci-assurance.yml` (risk register, staffing, schedule) as disproportionate for this repo's current maturity. Fixed `Enable-RepoIssueTracking.ps1` (REQ-004: `gh label create --force`, real failures now surfaced, not silently swallowed -- live-verified against the real repo) and `New-HumanEscalation.ps1` (REQ-005: verifies the marker is actually present in the returned issue body client-side, not trusting GitHub's search index alone; a search failure now warns instead of silently falling through -- live-verified: real create + real update-in-place on the same issue, confirmed via `gh issue view`, test issue closed after). Reworded REQ-003/REQ-021/REQ-024 to accurately scope their actual guarantees instead of overclaiming.
- **(`#80`) more Architecture Packet gaps + practice-vs-document gaps**: added 7 missing components (`Run-WindowsSbom.ps1`, `Run-LinuxSbom.ps1`, `Test-Environment.ps1`, `Test-RunnerAdministrator.ps1`, `winrm-service-bootstrap-installer.nsi` disclosed as an unwired research prototype, `Install-VerifiedNugetPackage.ps1`) plus 2 new containers (Release automation, Agent orientation) to the Component/Container Views. Fixed the Context View's stale `feature/*`-only claim and SHA-pinning overclaim. **Decision: adopt wiki release-tagging going forward** -- added a new Release Flow step tagging the wiki repo at the same `vX.Y.Z` as each main-repo release (this release is the first, `v0.3.31`). Reworded Release Flow Step 2 to disclose that `CHANGELOG.md` is typically already on `develop` before the release branch is cut. Corrected the wiki's own "branch-then-push, reviewed" claim (`cm-plan.md`, the wiki's `Home` page, the SRS) -- real git history shows zero branches were ever used; disclosed honestly instead of left overclaiming.
- **(`#52`/`#63`) REQ-018/REQ-019 merge + `configuration-schema.md` dangling refs**: merged REQ-019 into REQ-018 (its own Fit Criterion read "Same evidence as REQ-018," failing ISO/IEC/IEEE 29148 §5.2.5's "Necessary" characteristic) -- `REQ-019`'s ID formally retired, every cross-reference updated (`REQ-032`, the RTM, the Architecture Packet). Repointed `configuration-schema.md`'s 3 dangling references instead of authoring a new doc: `Run-Pipeline.ps1`'s pipeline-config error now cites `pipeline-config.schema.json` (the real, validated schema); its `VIPM_TOML_CONFIGS` errors now describe the expected shape inline; `add-project.yml`'s error summary now links to `Add-Project.ps1` itself.
- **(`#62`) triaged, not closed**: the critical all-phases-blocking bug was already resolved in round 3/4 (restored script, confirmed intentional `package-build` exception). Explicitly scoped the remaining work (live-dispatch-proving the other 9 phases, which requires a real multi-hour VM-provisioning cycle including a human-interactive LabVIEW activation step) as out of scope for this documentation-focused round -- a good candidate for a dedicated future VM-provisioning session.
- All 6 validation/syntax checks (JSON/YAML/XML/Python/bash/PowerShell) pass across every changed file; the full Pester suite (10 tests) and pytest suite (17 tests) pass; the local `release-gate` assurance scan shows all 5 required gates still `PASS` (High confidence, 99-100%), matching the established baseline.

## v0.3.30

- fix a real, previously-unnoticed ADR-immutability violation: round 5's own commit edited `docs/architecture/adr/ADR-0001-adopt-gitflow-for-release-governance.md`'s accepted Decision/Consequences text in place, directly contradicting `docs/information-item-map.md`'s own stated Decision Record convention ("a new `ADR-NNNN` file is added, never edited after acceptance"). Reverted the original accepted text to be byte-identical to its first acceptance commit, and moved round 5's genuinely useful content (the `v0.3.28` GitFlow-exception disclosure, the stamp-condition clarification) into a new, clearly-marked, append-only "Addendum" section instead
- fix 2 remaining dangling `docs/projectbuddy-consumption.md` references round 5 missed when it fixed 2 sibling instances of the identical gap: `Run-Pipeline.ps1`, `Run-Pipeline.sh`, `Run-WindowsSbom.ps1`, and `Run-LinuxSbom.ps1` (4 files, all repointed to README.md's real "Consumption model" section)
- fix `.github/skills/projectbuddy-identity/` (`SKILL.md`, `agents/openai.yaml`): both still asserted the unqualified "ships with zero pre-committed... state" claim after `README.md` was already corrected in round 5 to honestly disclose the real, tracked violations (`#68`) -- the same class of sibling-location drift this session keeps finding in this specific file pair
- fix `.github/workflows/standards-compliance-recheck.yml`'s header comment: still said public upstream "deliberately carries zero private infrastructure state" after `docs/architecture/standards-compliance.md` was corrected for the identical overclaim in round 5 (a real, online `vm-provisioner`-labeled runner is in fact registered against it)
- fix `docs/sbom-pipeline/procedure.md`: still claimed both containerized pipelines validate against `pipeline-config.schema.json` uniformly, contradicting the wiki Architecture Packet's own already-corrected disclosure that the Linux container does its own separate, narrower inline validation instead
- fix `docs/vm-provisioning/procedure.md`: (1) the receipt-verification-chain paragraph claimed a `package-build` receipt verifier exists; it doesn't (`package-vagrant-box.py` validates its *input* vagrant-box receipt but produces no independently-reverifiable receipt of its own output); (2) the Shared Schemas section implied both VM families' `provisioning-manifest.json` pin the same binary-hash categories, when Ubuntu's manifest has no Actions-runner/PowerShell/NSSM equivalents (Windows-specific concepts) -- clarified both are family-specific schemas, not one shared shape
- fix `docs/release/release-procedure.md`'s Hotfix Flow: added an explicit disclosure that this flow has never actually been exercised in this repo's real history (zero `hotfix/*` branches or PRs, ever), matching the same honesty standard already applied elsewhere in this document
- fix the wiki SRS's stale Document Control (`Version: Initial draft... as of v0.3.10`, `Status: Draft`) and 2 references to a `v0.3.11` tag that was never actually tagged or released (confirmed: tags jump from `v0.3.10` to `v0.3.13`) -- both now cite the real, verifiable commit (`c3a36ae`) that resolved the spec-to-CI wiring question
- fix the wiki Architecture Packet's Deployment View: round 5's own fix cited one specific, ephemeral historical workflow run ID as its sole evidence for `validate-vm-provisioning-dispatch.py`'s restoration -- directly contradicting this same SRS's own stated evidence-citation principle ("never at one specific historical workflow run ID -- a run ID is ephemeral and does not, by itself, prove anything about the current state of the code"). Reframed to cite the restored script's own reproducible code/logic as the primary evidence, with the historical dispatch noted only as a same-day live confirmation, not the proof itself
- fix the wiki Architecture Packet: disclose that `vm-fleet-status.yml`'s scheduled runs fail differently on the fork (`svelderrainruiz/projectbuddy`, which has no `vm-provisioner`-labeled runner at all, so every scheduled run queues and is cancelled) versus on public upstream (which now cleanly no-ops per round 5's fix) -- same root cause as the already-documented `standards-review-local` pattern, just not previously disclosed for this specific workflow
- fix REQ-010 (wiki SRS) and REQ-013 (wiki RTM): round 5 updated one of each requirement's two sibling locations (SRS or RTM) but not both, leaving the un-updated sibling stale -- REQ-010's SRS row now reflects the same `v0.3.28` exception already in its RTM row; REQ-013's RTM row now cites the actually-invoked `build-virtualbox.sh` instead of the orphaned, never-called `.ps1` sibling, matching its own SRS row
- added 2 new, previously-uncited `#68` (REQ-001 hardcoded-identifier) violation sites as evidence: `vm/windows-labview/unattended-template.xml` (a Windows-family instance, 4 hardcoded `<Description>` fields -- all prior evidence in that issue was Ubuntu-family), `vm/ubuntu-labview/provision-guest.sh`, and a broader, distinct `"sandbox-sbom-reporting/..."` schema-namespace-identifier pattern spanning 11 files (flagged with an important nuance: these are versioned, already-publicly-documented schema identifiers, not secrets -- likely an unfortunate namespace-naming choice rather than the same class of bug as the other sites, but still worth a deliberate decision)
- deleted 1 more already-merged stale branch on the fork
- filed `#80` (remaining lower-severity findings: more Architecture Packet Component/Container View gaps, Context View staleness around `feature/*` branch naming and consumer SHA-pinning precision, CM/release-procedure practice-vs-document gaps including the wiki's own lack of tag/commit-SHA binding to main-repo releases, and further ISO 29119-3/15289 formal-completeness instances compounding the already-open `#77`)
- all findings from this round's `--mode semantic-audit` pass (same 3-model delegation, explicitly asked to verify whether `v0.3.29`'s fixes held up and hunt for anything neither prior round caught): significantly fewer new defects than prior rounds (expected, 6 rounds deep into hardening the same spec/doc surface), but still real, previously-uncaught sibling-location drift and one genuine process violation (the ADR in-place edit) -- GPT found no new severe issues beyond already-tracked baselines; Gemini and Claude each independently caught several real, previously-missed sibling-drift defects, with meaningful overlap between the two (both independently found the ADR-0001 edit and the standards-compliance-recheck.yml staleness)

## v0.3.29

- fix `vm-fleet-status.yml`'s round-4 fix, which was itself incomplete: emitting an empty matrix (`{"include":[]}`) stopped the `jq` crash, but GitHub Actions marks the *entire workflow run* `failure` whenever a dependent job's `strategy.matrix` resolves to zero combinations -- a platform behavior the round-4 fix didn't account for (confirmed via real run history: 12 total runs since this workflow existed, 0 successes ever, including runs after the round-4 fix landed). Added an explicit `has-vms` output so `check-vm` is skipped entirely via `if:` when there are no VMs, instead of being handed an empty matrix
- fix `.github/workflows/release.yml`'s "Tag already exists" duplicate-tag check: `actions/checkout`'s default `fetch-tags: false` meant `git rev-parse "$VERSION"` could never see a real remote tag, making this safety check dead code since the very first version of this workflow. Replaced with `git ls-remote --exit-code --tags origin`, verified against both a real existing tag and a nonexistent one
- fix `.github/workflows/standards-assurance-scan.yml`: it cloned `repo-standards-review` at floating `HEAD` instead of the exact SHA this repo's compliance stamp is assessed against (silent drift risk), and its failure condition only checked the `req`/`arch` gates, silently ignoring a `cm`/`coverage`/`doc` regression. Pinned the clone to the stamped SHA and widened the failure check to all 5 required gates
- fix the wiki Architecture Packet's Deployment View: round 4's own fix (restoring `.github/scripts/validate-vm-provisioning-dispatch.py`) was never reflected here -- this section still said the script "does not exist" and every phase fails, directly contradicting round 4's own code change. Corrected to reflect the restoration, cited a real, first-ever successful `validate`-phase dispatch (`VITechnologies/projectbuddy` run `42335965127`) as positive evidence, and noted `package-build` remains blocked by design (consumer-vendored script), not by a missing file
- fix 2 new dangling `docs/projectbuddy-consumption.md` references introduced by round 4's own fix (a file already known-missing and tracked in `#63`) -- repointed both to README.md's real "Consumption model" section instead of making the same gap worse
- fix `.github/skills/projectbuddy-identity/` (`SKILL.md` and `agents/openai.yaml`): both still described `Run-Pipeline.sh` as a native SBOM pipeline entry point, claimed nonexistent `teardown`/`enable-vrde` phases for `provision-windows-labview-vm.yml` (the real phases are `validate`/`create-vm`/`install-vipm`/`install-labview`/`register-runner`, exactly 5), and showed the reusable-workflow consumption pattern as `@<tag>` instead of `@<commit-sha> # <tag>` -- the same class of defect fixed elsewhere in rounds 1-4, never caught in this agent-orientation file specifically until now
- fix `docs/architecture/standards-compliance.md`: it conflated two separate workflows, attributing `standards-compliance-recheck.yml`'s "Compare Stamped Version Against Latest" step to `standards-assurance-scan.yml` (which doesn't have that step); and claimed public upstream `VITechnologies/projectbuddy` "carries zero private infrastructure state" when it in fact has a real, online `vm-provisioner`-labeled self-hosted runner registered directly against it (the zero-state claim was about pre-committed configuration, not every runner ever registered)
- fix REQ-002 (wiki): still listed "ISO paths" as a required-with-no-default identity value after round 4 corrected this same overclaim in README.md/the Architecture Packet; fix REQ-001 (wiki): stopped asserting "Verified (manual)" against a Fit Criterion now known false (`#68`); fix REQ-010 (wiki RTM): reflects the one disclosed `v0.3.28` GitFlow exception rather than an unqualified "Verified"; fix REQ-013 (wiki): its cited evidence script (`vm/windows-labview/build-virtualbox.ps1`) is never actually called by any real workflow (confirmed via a repo-wide reference search) -- `provision-windows-labview-vm.yml` calls the sibling `.sh` script instead, which independently implements the same ISO SHA-256 guard the requirement describes
- fix `provision-windows-labview-vm.yml`'s `guest_full_name`/`guest_hostname` inputs: their own description text read "...no built-in default); required for create-vm" directly beside `required: false` and a real default value -- a literal self-contradiction in the same file. Also noted the cross-workflow inconsistency with `provision-ubuntu-vm.yml`, which marks the same conceptual fields `required: true`
- fix `docs/vm-provisioning/procedure.md`'s opening paragraph: claimed both VM families "share one discipline" requiring every identity value as `workflow_dispatch`-required, which is false (the two families disagree on `guest_full_name`/`guest_hostname` required-ness) and incorrectly grouped the ISO path in with identity values despite it having a legitimate generic default
- fix `docs/cm/cm-plan.md`: added `v0.3.0` to the tag/CHANGELOG mismatch disclosure (round 4 only caught `v0.2.2`); disclosed the `v0.3.28` GitFlow release-flow exception (a non-release PR landed directly on `main` after its release branch's own promotion, discovered mid-round while testing this round's own `release.yml` fix); noted the Hotfix Flow has never actually been exercised in this repo's real history; added a cross-reference for ISO/IEC/IEEE 12207 configuration-audit outcome (e) to this repo's own semantic-audit pipeline
- fix `docs/release/release-procedure.md` and `docs/architecture/adr/ADR-0001-adopt-gitflow-for-release-governance.md`: added the same `v0.3.28` exception disclosure and an explicit tagging-order rule to reduce recurrence; clarified ADR-0001's "every document carries a stamp" condition means coverage by the one canonical stamp, not N independent copies, and added real cross-references from `cm-plan.md`/`release-procedure.md` to that canonical stamp
- live-verified this round's `provision-ubuntu-vm.yml`/`release.yml`/`standards-assurance-scan.yml` fixes via real dispatches, not just local simulation (first-ever successful `provision-ubuntu-vm.yml` dispatch; a real draft-then-published release created end-to-end by the fixed `release.yml`; a fresh, successful `standards-assurance-scan.yml` run against current content)
- filed `#76` (round-4's `v0.3.24` CHANGELOG entry falsely claimed `Install-VerifiedNugetPackage.ps1` had "zero consumers anywhere in the pipeline" -- `SandboxSBOMReporting`'s `batch-pipeline.yml` calls it directly for a legitimate, unrelated purpose; added a protective warning at that repo's `PROJECTBUDDY_REF` pin site) and `#77` (remaining lower-severity findings: formal ISO 29119-3/15289 document-template completeness, further Architecture Packet component gaps, and 4 smaller REQ-level precision gaps)
- cleaned up 10 already-merged, undeleted sync branches on the fork
- all findings from this round's `--mode semantic-audit` pass (same 3-model delegation, explicitly asked to verify whether `v0.3.28`'s fixes held up and hunt for anything neither prior round caught): both GPT and Gemini independently found the same critical `vm-fleet-status.yml` empty-matrix regression and the same wiki self-contradiction; Claude independently confirmed both via real run-history queries and found 2 additional previously-uncaught defects (`release.yml`'s dead tag-check, REQ-013's orphaned evidence citation)

## v0.3.28

- fix a real, residual gap in `v0.3.27`'s own REQ-017 fix: only 1 of the 5 credential-cleanup sites in `provision-windows-labview-vm.yml` (`create-vm`) had actually been moved trap-before-write despite the `v0.3.27` CHANGELOG/compliance stamp claiming all 5 were fixed; the other 4 (`Attach Installer Media And Restart`, `install-vipm`, `install-labview`, `register-runner`) still wrote credential file(s) before installing the trap -- fixed all 5, re-verified with an automated step-boundary scanner (not manual re-reading, since manual reading had already missed this once) plus a live bash simulation of the exact failure scenario
- fix `.github/scripts/validate-vm-provisioning-dispatch.py`: restored this genuinely generic dispatch-contract validator to this engine, which was missing from the original ProjectBuddy extraction (confirmed by its real, unmodified presence in the `VITechnologies/SandboxSBOMReporting` consumer repo) -- `provision-ubuntu-vm.yml`'s "Validate Dispatch Contract" step previously failed unconditionally on every phase for every dispatch because this script did not exist here; local simulation confirms the step now passes for representative `validate`-phase inputs. `vm/ubuntu-labview/build-vip-package.sh` remains deliberately absent (confirmed as an intentional, disclosed, consumer-vendored exception, not a dropped file like the validator was) -- added an explicit doc comment to the `package-build` phase clarifying this (`#62` updated with the corrected root cause)
- fix `.github/workflows/release.yml`: it interpolated `${{ steps.changelog.outputs.notes }}`/`${{ inputs.version }}` directly into `run:` script bodies -- an expression-injection anti-pattern under a `contents: write` token, reproduced as a real bash syntax error using `v0.3.27`'s own CHANGELOG entry (18 embedded double quotes). Rewrote to pass all inputs via `env:` and write release notes to a file for `gh release create --notes-file`, verified end-to-end against the real `v0.3.27` entry
- fix `vm-fleet-status.yml`: its `generate-matrix` job called `jq` directly on `runner-inventory.json`, which is never committed in this public engine repo by design -- every scheduled run failed with a `jq: error: Could not open file` (confirmed via real run logs). Now emits an empty matrix and a clear `::notice::` instead of failing when the file is absent
- fix `README.md`: (1) the "every VM identity value... no built-in default" claim (script-level `build-virtualbox.ps1`/`.sh` parameters) didn't account for `Iso`/`GuestUser`/`WinRmHostPort` genuinely having generic, non-identity defaults; (2) `vm-fleet-status.yml` was incorrectly grouped with the two provisioning workflows as taking "every real identity value" as an explicit input, when it actually takes zero `workflow_dispatch` inputs; (3) the "zero pre-committed project configs" claim is contradicted by real, tracked violations (`#68`) -- added an explicit caveat; (4) the "never vendored/copied script files" claim omitted the already-accepted `#48` exception for `workflow_dispatch`-only workflows, which a consumer genuinely vendors its own copy of
- fix `Run-Pipeline.ps1`: its own Linux-guard error message said "On Linux, use Run-Pipeline.sh", actively pointing users at a script this same session already established is not a SBOM pipeline at all
- fix `docker/linux-sbom/linux_sbom_pipeline.py`: the Grype gate stage recorded `gate="passed"` even when `GRYPE_FAIL_ON=none` (i.e. not actually gated), diverging from the Windows container pipeline's more honest `"not-gated"` status for the same situation -- aligned Linux's reporting to match
- fix `.github/workflows/ci-assurance.yml`: pinned previously-unversioned `pip`/`pytest`/`pytest-cov`/`pyyaml` installs to the exact versions a real recent CI run proved against, and fixed a dangling `docs/requirements/srs.md` reference (that path only exists ephemerally during an active `standards-assurance-scan.yml` run, not as a permanently committed file) in both this workflow and `scripts/tests/test_convert_vipb_json.py`'s docstring, pointing both at the wiki's real, permanent SRS location instead
- fix `docs/cm/cm-plan.md`, `CONTRIBUTING.md`, and wiki REQ-010: the merge-back-to-`develop` no-op exception added in `v0.3.27` was only documented in `docs/release/release-procedure.md` -- propagated the same caveat to the other 3 places that described merge-back as unconditional
- fix `docs/cm/cm-plan.md`'s "every release" audit-trail claim: didn't account for pre-GitFlow releases (before `v0.3.9`), which were tagged under the prior trunk-based model per `ADR-0001` and have no `release/*` branch ancestry
- fix `docs/release/release-procedure.md`: (1) the GitFlow Branch Rules summary's hotfix merge-back bullet read as an either/or between `develop` and an active release branch, when the detailed Hotfix Flow steps are additive (always `develop`, plus an active release branch if one exists); (2) the Feature Flow only documented the older `sync-from-fork-<date>` branch-naming pattern, omitting the `sync-upstream-develop-*`/`sync-upstream-main-vX.Y.Z` patterns actually used in recent practice, and never explained the post-release fork-sync step that is why `develop`/`main` are usually already content-identical by the merge-back step
- fix the wiki Architecture Packet: (1) the Deployment View's Ubuntu-lifecycle caveat only said the package-ready snapshot was unreachable, when in fact every phase was unreachable (same root cause, now partly fixed above); (2) still called VIPM activation "LabVIEW-gated" after `v0.3.27`'s own REQ-032 correction established it isn't; (3) the Component View's `internal/Pipeline.Stages.ps1` row claimed 8 ordered stage functions including a preceding "policy gate" stage, when there are 6, and the policy-gate check is a helper called at the end of `Publish-DependencyTrack`, not a separate preceding stage; (4) the `package-vagrant-box.py`/receipt-scripts row claimed a package-ready receipt verifier exists; it doesn't; (5) the `vipm-exit-codes.json`/`pipeline-config.schema.json` row claimed both the native and both containerized pipelines validate against the schema, but the Linux container pipeline does its own separate, narrower inline validation instead; (6) the `build-virtualbox.ps1`/`.sh` rows repeated the same no-default overclaim fixed in README above; (7) added 6 previously-undocumented components to the Component View (`batch-provision.sh`, `Register-WindowsRunner.ps1`, `Register-WslRunnerKeepAlive.ps1`, `Test-SbomArtifact.ps1`, `scripts/Test-ToolchainPresence.ps1`+`toolchain-smoke-test.yml`, `scripts/Convert-VipbJson.py`)
- fix `docs/vm-provisioning/procedure.md`: resolved a dangling "tracked as a follow-up gap" reference for the hostname-collision risk (no issue actually tracked it) by filing `#69` and linking it; added a reachability caveat to the Ubuntu lifecycle phase list matching the Architecture Packet fix above
- file `#68`: a broader, previously-untracked REQ-001 violation -- hardcoded project-specific identifiers in `Register-WslRunnerKeepAlive.ps1`, `vm/ubuntu-labview/base-bootstrap.sh` and 5 other scripts sharing its `/SandboxSBOMReporting/SshHostEd25519Fingerprint` guest-property path, and `Run-WindowsSbom.ps1`/`Run-LinuxSbom.ps1`'s own `.DESCRIPTION` text -- beyond what `#62` already tracked
- file `#69`: `batch-provision.sh` gives every VM in a batch the same in-guest hostname/full name (a real collision risk, not safely auto-fixable given Windows's 15-character NetBIOS hostname limit)
- file `#70`: `ALLOW_PACKAGE_DRIFT`'s default is opposite between the native pipeline (off) and both containerized pipelines (on); REQ-008/REQ-009's "validated via real CI dispatch instead" justification doesn't hold for `toolchain-smoke-test.yml` (never actually called in this repo) or `Test-SbomArtifact.ps1` (pure offline code, never fixtured); compliance-scan "Known tradeoffs" correspondence overclaims its own evidence strength for physical-host/PDF-corpus claims
- all findings from this round's `--mode semantic-audit` pass (same 3-model delegation, explicitly asked to verify whether `v0.3.27`'s fixes held up and hunt for anything neither prior round caught): 2 real regressions found and fixed (the REQ-017 trap-ordering miss above, and this round's own corrected root-cause for `#62`); `#62`/`#63` updated with corrected evidence

## v0.3.27

- fix a real, residual gap in `v0.3.26`'s own REQ-017 fix: 2 of the 5 `trap ... EXIT` installations wrote their credential file(s) *before* installing the trap, so a failure during file creation itself (e.g. the register-runner step's PAT-file creation) could still leave an earlier-written file behind -- moved every trap installation to the very first executable line of its step, before any file is created, since `rm -f`/`shred -u` on a not-yet-created file are safe no-ops
- fix REQ-032 (wiki) and `docs/vm-provisioning/procedure.md`: the previous round claimed VIPM's automated activation is "gated on the operator having already completed LabVIEW's own interactive activation" -- this is false. `capture-activation.sh` runs VIPM activation unconditionally whenever a VIPM env file is supplied, with no check of LabVIEW's actual activation state; the precondition is a documented operator convention (the phase's usage text), not a runtime-enforced gate. Also corrected the real call chain (`activate-vipm.sh` is invoked by `capture-activation.sh`, not directly by `complete-activation.sh`)
- fix README.md: a second, still-present instance of the "LabVIEW/VIPM activation is human-only" overclaim (line 78, a different passage than the one already fixed) and a reintroduced "every ... required input" overclaim contradicting the same round's own "no built-in default" fix
- fix the wiki Architecture Packet: the Container/Component View tables still described `Run-Pipeline.ps1`/`.sh` together as the "Native SBOM pipeline" -- split into two rows, with `Run-Pipeline.sh` correctly described as the unrelated LabVIEWCLI tool it actually is (`README.md`/`docs/sbom-pipeline/procedure.md` were fixed in `v0.3.26`, the wiki was not)
- fix the wiki Architecture Packet's Deployment View: no longer asserts the Ubuntu package-build/signed-`.vip` capability is proven, since `provision-ubuntu-vm.yml`'s `package-build` phase cannot currently succeed (`#62`, open)
- fix wiki REQ-007: still listed `Run-Pipeline.sh` as a "native SBOM pipeline script" with Dependency-Track fail-closed behavior it doesn't implement (same class of gap as the Architecture Packet fix above)
- fix `docs/vm-provisioning/procedure.md`'s "no built-in default" wording: disclosed a real gap the previous round's correction didn't go far enough to catch -- `batch-provision.sh` never overrides `guest_hostname`/`guest_full_name` per VM, so every VM it creates in a single batch gets the identical in-guest hostname (`vm-builder.local`), a real same-hostname collision risk, not just a cosmetic placeholder
- fix `docs/sbom-pipeline/procedure.md`'s `ALLOW_PACKAGE_DRIFT` default claim: it said `=1` was the default; the real code only treats it as true when explicitly set to the literal string `"1"`, so the real default is off
- fix `docs/cm/cm-plan.md`'s unrendered `[[Home]]` wiki-link syntax (only the wiki itself supports `[[...]]`; replaced with a real link)
- fix `docs/release/release-procedure.md`: documented the already-practiced, legitimate no-op exception for the `release/vX.Y.Z -> develop` merge-back step when `develop`/`main` are already content-identical (this is what `v0.3.24`-`v0.3.26` actually did, correctly, but the procedure didn't say so)
- fix the published `v0.3.26` GitHub release notes, which were truncated by one bullet relative to the full `CHANGELOG.md` entry (a copy/paste range error, not a code change -- release notes text is editable, unlike the tag/assets)
- fix `docs/information-item-map.md`'s baseline (bumped to this version)
- all findings from this verification-focused re-run of `repo-standards-review@v0.2.23`'s `--mode semantic-audit` (same 3-model delegation, explicitly asked to verify whether `v0.3.26`'s fixes actually held up): most of `v0.3.26`'s fixes were confirmed genuinely SOLID this round (REQ-013/017/018/025/030, the `Run-Pipeline.sh` fix in README/sbom-pipeline docs, the CM Change Control disclosure); the gaps above are what a single-pass fix missed. `#62`/`#63` updated with additional evidence (a REQ-001-violating hardcoded package name in the same broken `package-build` phase; a missing `docs/projectbuddy-consumption.md`; REQ-013's dangling "tracked separately" reference, now actually linked)

## v0.3.26

- fix REQ-017 (again): `provision-windows-labview-vm.yml`'s credential-file cleanup only ran on the success path (`set -euo pipefail` means an earlier failure skips the end-of-step shred line entirely) -- replaced every end-of-step cleanup with a `trap ... EXIT` right after each file is written, so cleanup now fires on any exit path, success or failure, verified locally that explicit exit codes are preserved through the trap
- fix a real, 3-model-consensus documentation defect: `docs/sbom-pipeline/procedure.md` and `README.md` both mischaracterized `Run-Pipeline.sh` as "the native Linux SBOM pipeline" -- it is actually a separate, unrelated LabVIEWCLI MassCompile/VI Analyzer tool for NI's own example VIs, with zero SBOM/VIPM/Grype/DT stages (confirmed directly against the script's own header comment); there is no native Linux SBOM pipeline in this repo today, only the containerized one
- fix `docs/sbom-pipeline/procedure.md`'s native-pipeline stage list: added the missing "Packages" stage (`build.packages`, `vipm lock`, signed builds), and corrected the Policy-gate/Publish stage ordering (`Publish-DependencyTrack` calls `Test-PipelinePolicyGate` internally as its last action, not as a separate preceding stage)
- fix `docs/vm-provisioning/procedure.md`'s Ubuntu-LabVIEW `complete-activation` description: VIPM activation is genuinely automated (`activate-vipm.sh`'s `vipm activate --direct-vipm-io`, per `REQ-032`), only LabVIEW activation stays human-only -- the doc previously claimed both stayed human-only, the same scope error already fixed in the wiki SRS
- fix `docs/vm-provisioning/procedure.md`'s overclaimed "no built-in default" wording: several `workflow_dispatch` inputs do carry generic placeholder defaults (e.g. `vm_name`'s `example-windows11-labview2026-myproject`); none default to any real fork's actual identity, which was the real intent, but the literal wording was inaccurate
- fix `docs/cm/cm-plan.md`'s Change Control table: disclosed the real `gh pr merge --admin` bypass mechanism already documented in `CONTRIBUTING.md`, which the CM plan's "Operator review of the PR" claim omitted entirely
- fix `docs/information-item-map.md`'s stale `Baseline: v0.3.11` (14 releases stale)
- fix README.md's reusable-workflow pinning-format claim to match the real, recommended `@<commit-sha> # <tag>` format (it said `@<tag>` only, contradicting its own adjacent "Directly-invoked scripts" bullet and `toolchain-smoke-test.yml`'s own canonical example)
- narrow wiki SRS REQ-013's wording to the base-OS ISO specifically (the auxiliary NSIS/service-bootstrap bootstrap media it also implied coverage for is only existence-checked, not hash-verified -- a known, separately-tracked gap, `#63`)
- all findings from this round found by `repo-standards-review@v0.2.23`'s `--mode semantic-audit`, run across all 5 areas (REQ/ARCH/TEST/CM/DOC) with 3 independent models (Claude Opus, GPT-6-astra, Gemini-4-argon); the single most significant finding (`provision-ubuntu-vm.yml` has zero total dispatches ever and references 3 scripts that have never existed in this repo) needs an operator decision before it can be fixed -- filed as `#62`; `#63` tracks 2 smaller follow-ups

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

