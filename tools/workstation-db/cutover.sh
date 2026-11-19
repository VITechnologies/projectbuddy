#!/usr/bin/env bash
# Root step: move the native cluster to 55432 and stop (not remove) the Docker container, or undo that (#381).
#
#   sudo tools/workstation-db/cutover.sh cutover    # run after a fresh restore-from-container.sh passed, writers quiesced
#   sudo tools/workstation-db/cutover.sh rollback   # native back to the staging port, container started again
#
# The container and its volume are never removed. Both directions are idempotent.
set -euo pipefail
CONTAINER=rsr-registry-local-postgres
VERSION=16 CLUSTER=workstation STAGING=55433 LIVE=55432
UNIT="postgresql@$VERSION-$CLUSTER.service"
log() { printf '[cutover] %s\n' "$*"; }
[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }
mountpoint -q /run/media/sergio/Data1 || { echo "Data1 is not mounted" >&2; exit 1; }

wait_ready() { for _ in $(seq 1 30); do pg_isready -q -h 127.0.0.1 -p "$1" && return 0; sleep 1; done; echo "nothing ready on $1" >&2; return 1; }
set_port() {
  pg_conftool "$VERSION" "$CLUSTER" set port "$1"
  systemctl restart "$UNIT"
  wait_ready "$1" || { journalctl -u "$UNIT" -n 20 --no-pager >&2; return 1; }
}

case "${1:-}" in
  cutover)
    if pg_lsclusters -h | awk -v c="$CLUSTER" -v p="$LIVE" '$1==16 && $2==c && $3==p && $4=="online" {f=1} END{exit !f}'; then
      log "already cut over: native PostgreSQL is online on $LIVE"; exit 0
    fi
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
