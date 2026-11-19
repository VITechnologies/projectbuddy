#!/usr/bin/env bash
# Root step: move the native cluster to 55432 and stop (not remove) the Docker container, or undo that (#381).
#
#   sudo tools/workstation-db/cutover.sh cutover    # run right after a passing restore-from-container.sh (it refuses if the container was written to since)
#   sudo tools/workstation-db/cutover.sh rollback   # native back to the staging port, container started again
#
# The container and its volume are never removed. Both directions are idempotent.
set -Eeuo pipefail   # -E: the ERR trap must also fire inside functions such as set_port
CONTAINER=rsr-registry-local-postgres
VERSION=16 CLUSTER=workstation STAGING=55433 LIVE=55432
UNIT="postgresql@$VERSION-$CLUSTER.service"
OWNER="${SUDO_USER:-sergio}"
OWNER_HOME="$(getent passwd "$OWNER" | cut -d: -f6)"
STATE_FILE="${PB_RESTORE_STATE:-$OWNER_HOME/.local/state/projectbuddy-workstation-db/restore-state}"
PASSWORD_FILE="${PB_PASSWORD_FILE:-$OWNER_HOME/.config/rsr-registry-local/postgres-password}"
MAX_AGE_SECONDS="${PB_MAX_STATE_AGE:-900}"
PSQL="${PB_PSQL:-/usr/lib/postgresql/16/bin/psql}"
log() { printf '[cutover] %s\n' "$*"; }
[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }
mountpoint -q /run/media/sergio/Data1 || { echo "Data1 is not mounted" >&2; exit 1; }

wait_ready() { for _ in $(seq 1 30); do pg_isready -q -h 127.0.0.1 -p "$1" && return 0; sleep 1; done; echo "nothing ready on $1" >&2; return 1; }
set_port() {
  pg_conftool "$VERSION" "$CLUSTER" set port "$1"
  systemctl restart "$UNIT"
  wait_ready "$1" || { journalctl -u "$UNIT" -n 20 --no-pager >&2; return 1; }
}

# Refuse unless the source was not written to since the last verified restore, so no write is lost silently.
guard_no_writes_since_restore() {
  [ -r "$STATE_FILE" ] || { echo "no restore state at $STATE_FILE: run restore-from-container.sh --wipe-target first" >&2; return 1; }
  local epoch now age expected actual dbs
  epoch="$(sed -n 's/^epoch=//p' "$STATE_FILE")"
  now="$(date +%s)"
  age=$((now - epoch))
  [ "$age" -le "$MAX_AGE_SECONDS" ] || { echo "restore state is ${age}s old (max $MAX_AGE_SECONDS): re-run restore-from-container.sh --wipe-target" >&2; return 1; }
  expected="$(grep '|' "$STATE_FILE" | sort)"
  dbs="$(printf '%s\n' "$expected" | cut -d'|' -f1 | paste -sd, -)"
  actual="$(PGPASSWORD="$(tr -d '\r\n' < "$PASSWORD_FILE")" "$PSQL" -X -Atq -h 127.0.0.1 -p "$LIVE" -U postgres -d postgres -v ON_ERROR_STOP=1 \
    -c "select datname||'|'||(tup_inserted+tup_updated+tup_deleted) from pg_stat_database where datname = any(string_to_array('$dbs', ',')) order by datname" | sort)"
  if [ "$expected" != "$actual" ]; then
    echo "the container was written to after the verified restore; nothing changed. Re-run restore-from-container.sh --wipe-target, then cutover." >&2
    return 1
  fi
}

case "${1:-}" in
  cutover)
    if pg_lsclusters -h | awk -v c="$CLUSTER" -v p="$LIVE" '$1==16 && $2==c && $3==p && $4=="online" {f=1} END{exit !f}'; then
      log "already cut over: native PostgreSQL is online on $LIVE"; exit 0
    fi
    guard_no_writes_since_restore || exit 1
    # Any failure from here on restores the container, so the outage never outlives the script.
    undo() { log "cutover failed; restoring the previous state"; pg_conftool "$VERSION" "$CLUSTER" set port "$STAGING" || true; systemctl restart "$UNIT" || true; docker update --restart=unless-stopped "$CONTAINER" >/dev/null || true; docker start "$CONTAINER" >/dev/null || true; }
    trap undo ERR
    docker update --restart=no "$CONTAINER" >/dev/null   # a reboot must not bring the container back onto 55432
    docker stop "$CONTAINER" >/dev/null
    for _ in $(seq 1 15); do ss -ltn "sport = :$LIVE" | grep -q LISTEN || break; sleep 1; done
    if ss -ltn "sport = :$LIVE" | grep -q LISTEN; then echo "$LIVE is still in use" >&2; false; fi
    set_port "$LIVE"
    trap - ERR
    log "native PostgreSQL now on 127.0.0.1:$LIVE; container stopped, kept with its volume"
    ;;
  rollback)
    set_port "$STAGING"
    docker update --restart=unless-stopped "$CONTAINER" >/dev/null
    docker start "$CONTAINER" >/dev/null
    wait_ready "$LIVE"
    log "container is back on 127.0.0.1:$LIVE; native PostgreSQL on $STAGING"
    ;;
  *) echo "usage: $0 cutover|rollback" >&2; exit 2 ;;
esac
