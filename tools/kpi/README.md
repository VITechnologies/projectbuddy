# `tools/kpi/`: `projectbuddy-kpi`

`projectbuddy-kpi` measures what AI-assisted work costs, and computes the KPIs
that steer spec-driven design toward lower cost. It serves the north star
[#328](https://github.com/VITechnologies/projectbuddy/issues/328) and its
milestone K
([#330](https://github.com/VITechnologies/projectbuddy/issues/330)).

It reads the agent's own session event logs
(`~/.copilot/session-state/<session>/events.jsonl`) into a local PostgreSQL
database, by default `kpi_local`, schema `kpi_v1`. The logs and the database
stay on the workstation. Only relative figures are ever published.

```bash
dotnet build ProjectBuddy.Kpi -c Release -o out
out/projectbuddy-kpi migrate --create-database --port 55432 --password-file "$HOME/.config/<instance>/postgres-password"
out/projectbuddy-kpi ingest --port 55432 --password-file ...
out/projectbuddy-kpi track --repository owner/repo --forge github --port 55432 --password-file ...
out/projectbuddy-kpi resolve --port 55432 --password-file ...   # needs gh, and glab for GitLab
out/projectbuddy-kpi report --port 55432 --password-file ...
```

## Cost model

- **AI:** AI cost is metered in **AI Units (AIU)**, the unit the account quota
  is billed in (#339). The calibration:
  - a Copilot CLI run that reported 6.93 AIU used 7.0 quota units;
  - over a window of an agent session, the quota fell by 305.1. Its calls'
    exact AIU summed to 305.1, and their nominal costs to 615.

  USD = AIU × `usd_per_unit`: 0.01, the gross price of a Copilot AI credit in
  GitHub's billing usage report (#371). A usage record's `cost`
  (15 per claude-opus-5.5 call) is nominal and is kept as `nominal_units`.
- **Operator:** active time is the union of all sessions' spans, where a gap
  over `idle_gap_minutes` (30) ends a span. It is priced at the T1 rate.
  Recorded operator entries add time at their own tier's rate.
- **Prompts:** an `ask_user` prompt's wait is capped at `prompt_cap_minutes`
  (10) in the prompt breakdown.

| Tier | Role | USD/hour |
| --- | --- | --- |
| T1 | accountable owner | 150 |
| T2 | senior engineer and reviewer | 110 |
| T3 | engineer running runbooks | 75 |
| T4 | technician | 45 |

The settings and tiers are rows in `kpi_v1.settings` and `kpi_v1.tiers`.

## Commands

Every command prints one JSON object. Every command except `migrate` refuses a
database that lacks the embedded migrations.

| Command | What it does |
| --- | --- |
| `migrate [--create-database]` | Applies the embedded migrations. A migration that changed after it was applied is refused. |
| `ingest [--sessions <dir>]` | Reads every `<uuid>/events.jsonl` under the sessions directory (default `~/.copilot/session-state`). Facts are keyed by event or tool-call ID, so ingesting again changes nothing. A tool call that has completed since the last ingest is updated, and active spans are derived again. A line that is not a readable event refuses the ingest. |
| `track --repository <path> --forge <github\|gitlab>` | Adds a repository whose merged requests are accepted changes: `owner/repo` on GitHub, `group[/subgroup…]/project` on GitLab. |
| `resolve [--gh <path>] [--glab <path>]` | Lists every request each tracked repository merged since the first session, with `gh api repos/<repo>/pulls?state=closed` or `glab api projects/<path>/merge_requests?state=merged`, newest update first, until updates are older than the first session. Listing again changes nothing. A repository whose listing fails is reported in `unresolved` with its reason, and what was already listed is kept. A tool that needs other credentials for some repositories can run as a wrapper in a second pass, for example `GITLAB_TOKEN=… glab`. |
| `mark --session <id> --item <ref>` | Attributes the session's calls from now until its next marker to a work item. |
| `gap --session <id> --item <ref> --kind <kind> --fact <text> --belongs-in <document>` | Records a spec gap: a fact the agent had to discover that the specification should have stated. The kind is `code-read`, `operator-question` or `failed-run`. |
| `review --session <id> --item <ref> --round <n> --findings <n>` | Records an independent review round and how many findings it produced. |
| `requirements --item <ref> --count <n>` | Records how many requirements a work item added or changed. |
| `operator --tier <T> --minutes <n> --activity <activity> [--item <ref>] [--note <text>]` | Records operator time outside sessions. The activity is `prompts`, `approvals`, `technician`, `review`, `watching` or `restarts`. |
| `report` | Prints the KPIs. |

A work item is `owner/repo#N` (an issue or pull request) or
`group[/subgroup…]/project!N` (a merge request).

Exit codes: 0 done, 2 usage, 3 refused, 5 database error. A check constraint,
such as a malformed item, is a database error.

## What is read from a log

- **Model calls:**
  - **Agent (SDK) sessions:** one call per `session.usage_record`, with its
    initiator (user, agent or sub-agent), model, nominal cost and tokens.
  - **Copilot CLI sessions,** which have no usage records: one call per
    `assistant.message`. A sub-agent's messages name the tool call that
    started it, and the nominal cost is 0.
- **AIU:** measured, not estimated.
  - **Exact per call:** an agent session's usage record carries its call's
    exact billed AIU (`copilotUsage.totalNanoAiu`).
  - **Session totals, agent sessions:** the deltas of the cumulative
    `session.usage_checkpoint` `totalNanoAiu`. A decrease starts a new counter.
    A fork's counter continues from the parent's running total at the fork:
    the last copied checkpoint plus the copied calls after it.
  - **Session totals, CLI sessions:** each process's `session.shutdown` total.
  - **Shares:** in each measured interval, what the calls' exact AIU don't
    account for is shared among the calls without an exact value. That means
    every CLI call, and older records. The weights are price-weighted tokens:
    uncached input 1, cache writes 1.25, cache reads 0.1 and output 5. CLI
    calls are weighted by output tokens alone, and calls without tokens get
    equal shares.
  - **Unattributed overhead:** what no call takes, such as helper-model calls
    that the log records no usage for, stays in the session's measured total.
    It is the difference between `measuredAiu` and `aiu`.
  - **Unmeasured calls:** a call without an exact value after the session's last
    measurement has no AIU until a later ingest measures it. The report counts
    these calls by comparing `measuredCalls` with `calls`.
- **Category:** what each call asked for, by the tools it requested:
  - `ask` (`ask_user`);
  - `edit` (`edit` or `create`);
  - `delegate` (`task` or `write_agent`);
  - `run` (`bash`);
  - `memory`;
  - `explore` (any other tool, all read-only);
  - `respond` (no tool).
- **Tool calls:** start and completion, paired by tool-call ID.
- **Operator messages:** `user.message` events.
- **Forked sessions:** a fork's log starts with its own `session.start` and
  then repeats its parent's history. Events dated before that start belong to
  the parent and are skipped.

## Accepted changes

An accepted change is a request that a tracked repository merged while a
session was active. Its merge time from `resolve` must lie within
`merge_skew_minutes` (2) of one of the session's active spans. The change is
accepted at that merge time.

- **Why the forge decides:** the logs can't say reliably whether a merge
  happened. `gh` prints its confirmation only to a terminal, and a blocked
  merge's advice mentions "merged". Governed tools, helper scripts, API calls
  and auto-merge merge without a parseable merge command. The forge's record
  covers them all.
- **Concurrent sessions:** if several sessions were active, the one whose
  latest tool call started at or before the merge accepts it.
- **Outside sessions:** a merge while no session was active, such as an
  overnight auto-merge, is not a session's acceptance. Its spend stays with the
  session's next accepted change. The report counts these merges as
  `outsideSessions`.

## KPIs

| KPI | Definition |
| --- | --- |
| Cost per accepted change | What the change's session spent after its previous accepted change, or the session's start, up to the acceptance: model calls plus that session's active time at T1. The report gives the count, mean and median, and the ten most recent changes. |
| Cost per requirement | The AI cost of marked work items, divided by their recorded requirement counts. |
| First-pass yield | The share of first review rounds with zero findings. |
| Rework | The AIU a marked work item spent after its first review. |
| Spec-gap rate | Recorded spec gaps per accepted change. |
| Exploration share | The share of AIU spent on `explore` calls. Exploration done through `bash` counts as `run`. |

The report also gives AIU by initiator, model and category, the nominal units, and the sessions' measured AIU. Under `merges`
it gives the tracked repositories, the merges listed, and the merges outside
sessions.

## Tests

The tests need a PostgreSQL 16 server. Each test creates its own
`kpi_test_<guid>` database and drops it afterwards.

```bash
# PBK_TEST_HOST and PBK_TEST_USER are optional.
export PBK_TEST_PASSWORD_FILE=/path/to/password PBK_TEST_PORT=55432
dotnet test ProjectBuddy.Kpi.Tests --settings coverage.runsettings --collect:"XPlat Code Coverage"
```

They write synthetic event logs and cover:

- every command and refusal;
- every call category;
- a forked session's repeated history;
- the forge's listings, through a stand-in `gh` and `glab` script:
  - paging, and stopping at updates older than the first session;
  - merges while one session, two sessions or no session was active, and
    within the skew;
  - closed unmerged requests, unreadable answers, and a failing or missing
    tool;
- idempotent re-ingest and re-listing, and completing an open tool call;
- overlapping sessions;
- every KPI, with exact values.

CI runs them as `KPI Tool Tests` against the pinned `postgres:16-alpine`
image, at 100% line and branch coverage.
