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
out/projectbuddy-kpi report --port 55432 --password-file ...
```

## Cost model

- **AI:** every model call is billed. Its `cost` comes from
  `session.usage_record` and is counted in premium units; the account quota
  reconciles the total. USD = units × `usd_per_unit` (0.04).
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
| `mark --session <id> --item <ref>` | Attributes the session's calls from now until its next marker to a work item. |
| `gap --session <id> --item <ref> --kind <kind> --fact <text> --belongs-in <document>` | Records a spec gap: a fact the agent had to discover that the specification should have stated. The kind is `code-read`, `operator-question` or `failed-run`. |
| `review --session <id> --item <ref> --round <n> --findings <n>` | Records an independent review round and how many findings it produced. |
| `requirements --item <ref> --count <n>` | Records how many requirements a work item added or changed. |
| `operator --tier <T> --minutes <n> --activity <activity> [--item <ref>] [--note <text>]` | Records operator time outside sessions. The activity is `prompts`, `approvals`, `technician`, `review`, `watching` or `restarts`. |
| `report` | Prints the KPIs. |

A work item is `owner/repo#N` (an issue or pull request) or `owner/repo!N` (a
merge request).

Exit codes: 0 done, 2 usage, 3 refused, 5 database error. A check constraint,
such as a malformed item, is a database error.

## What is read from a log

- **Model calls:** one per `session.usage_record`, with its initiator (user,
  agent or sub-agent), model, cost and tokens. Its **category** is what the
  call asked for, by the tools it requested:
  - `ask` (`ask_user`);
  - `edit` (`edit` or `create`);
  - `delegate` (`task` or `write_agent`);
  - `run` (`bash`);
  - `memory`;
  - `explore` (any other tool, all read-only);
  - `respond` (no tool).
- **Tool calls:** start and completion, paired by tool-call ID.
- **Accepted changes:** a successful bash call whose output says "merged"
  accepts every `gh pr merge <n> … --repo <owner/repo>` and
  `glab mr merge <n> … -R|--repo <owner/repo>` in its command. A merge without
  its repository cannot be attributed and is skipped. A change is counted once,
  at its first acceptance in any session.
- **Operator messages:** `user.message` events.

## KPIs

| KPI | Definition |
| --- | --- |
| Cost per accepted change | What the change's session spent after its previous accepted change, or the session's start, up to the acceptance: model calls plus that session's active time at T1. The report gives the count, mean and median, and the ten most recent changes. |
| Cost per requirement | The AI cost of marked work items, divided by their recorded requirement counts. |
| First-pass yield | The share of first review rounds with zero findings. |
| Rework | The units a marked work item spent after its first review. |
| Spec-gap rate | Recorded spec gaps per accepted change. |
| Exploration share | The share of units spent on `explore` calls. Exploration done through `bash` counts as `run`. |

The report also gives units by initiator, model and category.

## Tests

The tests need a PostgreSQL 16 server. Each test creates its own
`kpi_test_<guid>` database and drops it afterwards.

```bash
export PBK_TEST_PASSWORD_FILE=/path/to/password PBK_TEST_PORT=55432 # PBK_TEST_HOST and PBK_TEST_USER are optional
dotnet test ProjectBuddy.Kpi.Tests --settings coverage.runsettings --collect:"XPlat Code Coverage"
```

They write synthetic event logs and cover:
- every command and refusal;
- every call category and every merge form;
- idempotent re-ingest, and completing an open tool call;
- overlapping sessions;
- a change accepted twice;
- every KPI, with exact values.

CI runs them as `KPI Tool Tests` against the pinned `postgres:16-alpine`
image, at 100% line and branch coverage.
