# Workstation database (native PostgreSQL 16)

Native PostgreSQL 16 (PGDG) on Data1, replacing Docker container `rsr-registry-local-postgres` (#381). Data directory
`/run/media/sergio/Data1/postgresql/16/workstation`; systemd unit `postgresql@16-workstation` with `RequiresMountsFor` on Data1.

| Step | Who | Command |
|---|---|---|
| Install (staging port 55433) | root | `sudo tools/workstation-db/install-postgres16.sh --staging-port 55433` |
| Restore and verify | user | `tools/workstation-db/restore-from-container.sh [--wipe-target]` |
| Cutover to 55432 | root | `sudo tools/workstation-db/cutover.sh cutover` |
| Rollback | root | `sudo tools/workstation-db/cutover.sh rollback` |

`restore-from-container.sh` copies roles (`pg_dumpall --roles-only`) and every non-template database (`pg_dump -Fc --create`) from
the container, which stays read-only, and then compares source and target: roles and attributes, password verifiers,
memberships, database owners and ACLs (PUBLIC `CONNECT`/`TEMPORARY` revoked), per-role settings, schema/table/function grants and
exact row counts per table. It exits non-zero on any difference. It is re-runnable with `--wipe-target`. Dumps live in a 0700
temporary directory on tmpfs and are deleted on exit.

## Cutover window
1. `restore-from-container.sh --wipe-target` must end with `ALL CHECKS PASSED`. It then records each database's write counters
   (`tup_inserted + tup_updated + tup_deleted` from the container's `pg_stat_database`, read before the first dump) in
   `~/.local/state/projectbuddy-workstation-db/restore-state` (no secrets).
2. Within 15 minutes, `sudo tools/workstation-db/cutover.sh cutover`. Before it touches anything it re-reads those counters from
   the container and refuses if any differ (a client wrote after the verified copy) or the state file is missing or older than
   15 minutes. Then re-run step 1 and step 2 (`TRUNCATE` is not counted, so writers must still not truncate in the window); quiescing writers first makes that succeed.
3. On success it disables the container's restart policy, stops the container, and moves the native cluster to 55432. Any failure
   after the stop restores the container and the staging port. Re-running after success is a no-op.
4. Smoke test the clients (`projectbuddy-kpi quota`, farm, registry).

`restore-from-container.sh --verify-only` re-runs the comparison without copying anything. Tests: `tools/workstation-db/tests/test-cutover.sh`
(stubbed tools; covers failure rollback, the counter guard, the stale state file, re-run and rollback).

## Rollback
`sudo tools/workstation-db/cutover.sh rollback` puts the native cluster back on 55433 and starts the container (restart policy
restored) with its untouched volume. Writes made on the native service after the cutover are not in the container; re-dump them
first if they matter. Keep the container and its volume for at least 7 days after the cutover.
