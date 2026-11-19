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
1. The orchestrator quiesces writers (`kpi_local`, `farm_local`, registry tools).
2. `restore-from-container.sh --wipe-target` must end with `ALL CHECKS PASSED` (this takes the final, consistent copy).
3. `sudo tools/workstation-db/cutover.sh cutover`: disables the container's restart policy, stops it, moves the native cluster to 55432.
4. Smoke test the clients (`projectbuddy-kpi quota`, farm, registry), then release the writers.

## Rollback
`sudo tools/workstation-db/cutover.sh rollback` puts the native cluster back on 55433 and starts the container (restart policy
restored) with its untouched volume. Writes made on the native service after the cutover are not in the container; re-dump them
first if they matter. Keep the container and its volume for at least 7 days after the cutover.
