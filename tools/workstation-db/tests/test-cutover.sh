#!/usr/bin/env bash
# Behaviour tests for cutover.sh against stubbed docker/systemd/PostgreSQL tools (no root, nothing real is touched).
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/../cutover.sh"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
mkdir -p "$T/bin" "$T/s"
fails=0

stub() { printf '#!/usr/bin/env bash\n%s\n' "$2" > "$T/bin/$1"; chmod +x "$T/bin/$1"; }
S="$T/s"
stub id 'echo 0'
stub mountpoint 'exit 0'
stub sleep 'exit 0'
stub journalctl 'exit 0'
stub docker 'echo "docker $*" >> '"$S"'/calls
case "$1" in
  update) echo "${2#--restart=}" > '"$S"'/policy ;;
  stop) echo stopped > '"$S"'/container ;;
  start) echo running > '"$S"'/container ;;
esac'
stub pg_conftool 'echo "$5" > '"$S"'/port; echo "conftool $*" >> '"$S"'/calls'
stub pg_lsclusters 'echo "16 workstation $(cat '"$S"'/port) online /d /l"'
stub systemctl 'echo "systemctl $*" >> '"$S"'/calls
if [ "$1" = restart ] && [ -n "${STUB_FAIL_RESTART_ON:-}" ] && [ "$(cat '"$S"'/port)" = "$STUB_FAIL_RESTART_ON" ]; then exit 1; fi'
stub pg_isready 'p="${@: -1}"; [ "$p" = "$(cat '"$S"'/port)" ] && exit 0; [ "$p" = 55432 ] && [ "$(cat '"$S"'/container)" = running ] && exit 0; exit 1'
stub ss 'if [ "$(cat '"$S"'/container)" = running ] || [ "$(cat '"$S"'/port)" = 55432 ]; then echo "LISTEN 0 4096 127.0.0.1:55432 0.0.0.0:*"; fi'
stub psql 'cat '"$S"'/counters'
echo secret > "$T/pw"

reset() {
  echo 55433 > "$S/port"; echo running > "$S/container"; echo unless-stopped > "$S/policy"; : > "$S/calls"
  printf 'kpi_local|10\nfarm_local|3\n' > "$S/counters"
  printf 'epoch=%s\nfarm_local|3\nkpi_local|10\n' "$(date +%s)" > "$T/state"
}
run() {
  PATH="$T/bin:$PATH" SUDO_USER=root PB_RESTORE_STATE="$T/state" PB_PASSWORD_FILE="$T/pw" PB_PSQL="$T/bin/psql" \
    "$SCRIPT" "$@" > "$T/out" 2>&1
}
expect() { if [ "$2" = "$3" ]; then echo "ok   $1"; else echo "FAIL $1 (expected '$3', got '$2')"; fails=1; fi; }
state() { echo "port=$(cat "$S/port") container=$(cat "$S/container") policy=$(cat "$S/policy")"; }

reset; run cutover; expect "cutover succeeds" "$?" 0
expect "cutover result" "$(state)" "port=55432 container=stopped policy=no"
before="$(wc -l < "$S/calls")"
run cutover; expect "re-run after success exits 0" "$?" 0
expect "re-run after success changes nothing" "$(wc -l < "$S/calls")" "$before"
expect "re-run state" "$(state)" "port=55432 container=stopped policy=no"

reset; printf 'kpi_local|11\nfarm_local|3\n' > "$S/counters"; run cutover; r=$?
expect "write counter mismatch is refused" "$([ "$r" -ne 0 ] && echo refused)" refused
expect "mismatch changes nothing" "$(state) docker_calls=$(grep -c '^docker' "$S/calls")" "port=55433 container=running policy=unless-stopped docker_calls=0"

reset; rm "$T/state"; run cutover; r=$?
expect "missing state file is refused" "$([ "$r" -ne 0 ] && echo refused)" refused
expect "missing state changes nothing" "$(state)" "port=55433 container=running policy=unless-stopped"

reset; printf 'epoch=%s\nfarm_local|3\nkpi_local|10\n' "$(( $(date +%s) - 3600 ))" > "$T/state"; run cutover; r=$?
expect "stale state file is refused" "$([ "$r" -ne 0 ] && echo refused)" refused
expect "stale state changes nothing" "$(state)" "port=55433 container=running policy=unless-stopped"

reset; STUB_FAIL_RESTART_ON=55432 run cutover; r=$?
expect "failing systemctl restart makes cutover fail" "$([ "$r" -ne 0 ] && echo failed)" failed
expect "failure restores container and port" "$(state)" "port=55433 container=running policy=unless-stopped"
expect "undo ran" "$(grep -c 'cutover failed' "$T/out")" 1

reset; run cutover; run rollback; expect "rollback succeeds" "$?" 0
expect "rollback state" "$(state)" "port=55433 container=running policy=unless-stopped"

[ "$fails" = 0 ] && echo "ALL TESTS PASSED" || { echo "TESTS FAILED"; exit 1; }
