# Workstation hooks (`tools/hooks`)

User-level GitHub Copilot hooks plus the `projectbuddy-hooks` CLI (#381). They write to the workstation PostgreSQL (`kpi_local`,
schema `hooks`, migration `tools/kpi/ProjectBuddy.Kpi/Sql/0006_hooks.sql`) so that sessions spend no AI calls on bookkeeping.

## What the hooks do

| Event | Inline (one `psql` round trip, fails open within 1.5 s) | Detached (outside the hook's budget) |
|---|---|---|
| `sessionStart` | `hooks.on_start`: registers the session (id, workspace, status `running`) | `pb-kpi-sync`: KPI `ingest`, `mark`, `resolve` |
| `userPromptSubmitted` | `hooks.on_prompt`: session `running`; an open decision of this session is answered by the prompt | none |
| `agentStop` | `hooks.on_stop`: session `waiting` | `pb-stop-settle`: reads the turn's last assistant message once Copilot has flushed it and marks the session `done` (`DONE:`) or opens a decision (`DECISION NEEDED:`); `pb-kpi-sync` |

`preToolUse`, `postToolUse` and `postToolUseFailure` are deliberately not hooked: no cost on the tool path.
Copilot flushes the transcript after `agentStop` fires, which is why the message is read in a second step. `mark` uses only the
`work_ref` in the session's registry row, which the orchestrator writes with `sessions-set`; with none, nothing is marked.
`sessions-set --work-ref` itself writes a marker when a session's ref is first set (backdated to the session's `started_at`) or changed (now); an unchanged ref writes none, and `mark` skips an item that is already the session's latest.
`pb-kpi-sync` ingests and resolves at most once a minute and one run at a time.

Fail-open rules: every hook exits 0 and prints nothing; the database password is read from a 0600 file (never argv, never logged);
`PB_HOOKS_DISABLE=1` or the file `~/.config/projectbuddy-hooks/disabled` turns them off; errors go to
`~/.local/state/projectbuddy-hooks/hooks.log`. Only `~/.copilot/session-state/*/events.jsonl` is ever read.

## Install (two steps; the second affects every session)
```
projectbuddy-kpi migrate ...                       # applies 0006_hooks.sql to kpi_local
tools/hooks/install.sh install --admin-password-file ~/.config/rsr-registry-local/postgres-password \
    --kpi-bin <path to projectbuddy-kpi> --kpi-password-file ~/.config/rsr-registry-local/postgres-password
tools/hooks/install.sh activate                    # writes ~/.copilot/hooks/projectbuddy.json
tools/hooks/install.sh deactivate                  # removes it (rollback); the database objects stay
```
`install` creates the login role `pb_hooks` (member of `hooks_writer`, which can only call the hook functions) and the scripts, but no
hook is live until `activate`. A hook fires only for sessions started after `activate`. Full rollback: `deactivate`, then
`DROP SCHEMA hooks CASCADE; DROP ROLE pb_hooks;`.

## `projectbuddy-hooks` (orchestrator CLI)
```
sessions [--status running|waiting|done]          # the registry, with open decision counts
sessions-set --session <id> [--work-ref owner/repo#N] [--parent <id>] [--title <text>]
decisions [--all]                                 # the open DECISION NEEDED queue
answer --id <n> --text <text>                     # records an answer (the prompt that follows also answers it)
handoff-read --work-ref owner/repo#N              # prints updated_at and body of the issue
handoff-write --work-ref owner/repo#N --expected <updated_at> --body-file <file> [--session <id>]
```
All take `--password-file <path>` and optionally `--host --port --user --database` (default `127.0.0.1:5432`, `postgres`, `kpi_local`).
`handoff-write` takes a PostgreSQL advisory lock per work item, compares the issue's current `updatedAt` with `--expected` and
refuses (exit 3) if it moved, so a stale copy cannot overwrite a newer rewrite; on success it runs `gh issue edit` and records the
new `updatedAt`. Exit codes: 2 usage, 3 refused, 5 database, 6 `gh`.

## Tests
`dotnet test ProjectBuddy.Hooks.Tests` (100% line and branch gate in CI) and `tests/test-hook.sh`, `tests/test-install.sh`, all against a
throwaway PostgreSQL via `PBK_TEST_HOST/PORT/USER/PASSWORD_FILE`.
