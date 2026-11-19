#!/usr/bin/env bash
# Behaviour tests for scripts/pb-hook, pb-stop-settle and pb-kpi-sync against a real PostgreSQL (PBK_TEST_HOST/PORT/USER/PASSWORD_FILE, as the
# .NET tests use). Each run gets its own database and a fake HOME; nothing outside them is touched.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
BIN="$HERE/../scripts"
SQL="$HERE/../../kpi/ProjectBuddy.Kpi/Sql/0006_hooks.sql"
: "${PBK_TEST_PASSWORD_FILE:?set PBK_TEST_PASSWORD_FILE (and PBK_TEST_HOST/PORT/USER)}"
export PGHOST="${PBK_TEST_HOST:-127.0.0.1}" PGPORT="${PBK_TEST_PORT:-5432}" PGUSER="${PBK_TEST_USER:-postgres}"
PGPASSWORD="$(tr -d '\r\n' < "$PBK_TEST_PASSWORD_FILE")"; export PGPASSWORD
SUFFIX="$(head -c 6 /dev/urandom | od -An -tx1 | tr -d ' \n')"
DB="hooks_hook_$SUFFIX" ROLE="pb_hooks_$SUFFIX"
T="$(mktemp -d)"
cleanup() {
  psql -X -q -d postgres -c "drop database if exists \"$DB\" with (force)" -c "drop role if exists \"$ROLE\"" > /dev/null 2>&1
  rm -rf "$T"
}
trap cleanup EXIT
fails=0
expect() { if [ "$2" = "$3" ]; then echo "ok   $1"; else echo "FAIL $1 (expected '$3', got '$2')"; fails=1; fi; }
q() { psql -X -At -d "$DB" -c "$1"; }

psql -X -q -d postgres -c "create database \"$DB\"" || exit 1
psql -X -q -v ON_ERROR_STOP=1 -d "$DB" -f "$SQL" || exit 1
printf '%s\n' "role-pw-$SUFFIX" > "$T/pw"
psql -X -q -d postgres -c "create role \"$ROLE\" login password 'role-pw-$SUFFIX' in role hooks_writer" || exit 1

export HOME="$T/home"; mkdir -p "$HOME/.config/projectbuddy-hooks" "$HOME/.copilot/session-state"
S1=aaaaaaaa-1111-2222-3333-000000000001
S2=bbbbbbbb-1111-2222-3333-000000000002
cat > "$HOME/.config/projectbuddy-hooks/env" <<ENV
export PB_HOOKS_PASSWORD_FILE="$T/pw" PGHOST="$PGHOST" PGPORT="$PGPORT" PGDATABASE="$DB" PGUSER="$ROLE"
ENV
ENVFILE="$HOME/.config/projectbuddy-hooks/env"

hook() { # event, payload; prints the exit code and stdout length
  local out
  out="$(printf '%s' "$2" | "$BIN/pb-hook" "$1" 2> /dev/null)"; echo "$? ${#out}"
}
transcript() { mkdir -p "$HOME/.copilot/session-state/$1"; printf '%s\n' "$2" > "$HOME/.copilot/session-state/$1/events.jsonl"; }

expect "sessionStart exits 0 and prints nothing" "$(hook sessionStart "{\"sessionId\":\"$S1\",\"timestamp\":1,\"cwd\":\"/work/x\",\"source\":\"new\"}")" "0 0"
expect "sessionStart registers the session" "$(q "select status||'|'||workspace from hooks.sessions where session_id='$S1'")" "running|/work/x"

expect "snake_case payloads work too" "$(hook userPromptSubmitted "{\"session_id\":\"$S2\",\"prompt\":\"hi\"}")" "0 0"
expect "a prompt registers an unseen session" "$(q "select status from hooks.sessions where session_id='$S2'")" "running"

big="$(head -c 300000 /dev/zero | tr '\0' 'x')"
expect "a 300 KB prompt still goes through" "$(hook userPromptSubmitted "{\"sessionId\":\"$S2\",\"prompt\":\"$big\"}")" "0 0"
expect "the 300 KB prompt left the session running" "$(q "select status from hooks.sessions where session_id='$S2'")" "running"

iso() { date -u -d "@$(( $1 / 1000 )).$(printf %03d $(( $1 % 1000 )))" +%Y-%m-%dT%H:%M:%S.%3NZ; }
turn() { # session, content, [messageId], [turn_end time in ms]: a finished turn after a prompt, as Copilot writes it
  mkdir -p "$HOME/.copilot/session-state/$1"
  local end_iso; end_iso="$(iso "${4:-$(( $(date +%s%3N) + 100 ))}")"
  node_json() { python3 -c 'import json,sys;print(json.dumps({"type":"assistant.message","data":{"messageId":sys.argv[2],"content":sys.argv[1]}}, separators=(",",":")))' "$1" "$2"; }
  { echo '{"type":"user.message","data":{"content":"go"}}'
    echo '{"type":"assistant.turn_start","data":{"turnId":"0"}}'
    node_json "$2" "${3:-m-$RANDOM}"
    echo "{\"type\":\"assistant.turn_end\",\"data\":{\"turnId\":\"0\"},\"id\":\"e\",\"timestamp\":\"$end_iso\"}"
    echo '{"type":"hook.start","data":{}}'; } > "$HOME/.copilot/session-state/$1/events.jsonl"
}
stop_payload() { echo "{\"sessionId\":\"$1\",\"timestamp\":$(( $(date +%s) * 1000 )),\"cwd\":\"/work/x\",\"transcriptPath\":\"$HOME/.copilot/session-state/$1/events.jsonl\",\"stopReason\":\"end_turn\"}"; }
settle() { PB_SETTLE_TRIES=${3:-2} "$BIN/pb-stop-settle" "$1" "$HOME/.copilot/session-state/$1/events.jsonl" "$2" > /dev/null 2>&1; echo $?; }
now_ms() { echo $(( $(date +%s) * 1000 )); }

turn $S1 "DECISION NEEDED: A or B? \"quoted\" and 'single'" msg-1
expect "agentStop exits 0 and prints nothing" "$(hook agentStop "$(stop_payload $S1)")" "0 0"
expect "inline, the session only waits (the transcript is not flushed yet)" "$(q "select status from hooks.sessions where session_id='$S1'")" "waiting"
for _ in $(seq 1 40); do [ "$(q "select count(*) from hooks.decisions where session_id='$S1'")" = 1 ] && break; sleep 0.25; done
expect "the settle step opens the decision the turn ended with" "$(q "select count(*)||'|'||status from hooks.decisions where session_id='$S1' group by status")" "1|open"
expect "the decision text is intact" "$(q "select message from hooks.decisions where session_id='$S1'")" "DECISION NEEDED: A or B? \"quoted\" and 'single'"

expect "the next prompt answers the decision" "$(hook userPromptSubmitted "{\"sessionId\":\"$S1\",\"prompt\":\"take A\"}")" "0 0"
expect "answered by the prompt" "$(q "select status||'|'||answer from hooks.decisions where session_id='$S1'")" "answered|take A"
expect "a repeated settle of the same message opens nothing new" "$(settle $S1 "$(now_ms)")" "0"
expect "and the decision stays answered" "$(q "select count(*)||'|'||min(status) from hooks.decisions where session_id='$S1'")" "1|answered"

turn $S1 "DONE: landed" msg-2
hook userPromptSubmitted "{\"sessionId\":\"$S1\",\"prompt\":\"continue\"}" > /dev/null
expect "a settle for a stale stop is ignored once the session moved on" "$(settle $S1 "$(( $(now_ms) - 60000 ))")" "0"
expect "the session was not marked done by it" "$(q "select status from hooks.sessions where session_id='$S1'")" "running"
expect "settle marks DONE" "$(settle $S1 "$(now_ms)")" "0"
expect "the session is done" "$(q "select status from hooks.sessions where session_id='$S1'")" "done"

mkdir -p "$HOME/.copilot/session-state/$S2"
printf '%s\n' '{"type":"user.message","data":{"content":"go"}}' '{"type":"assistant.turn_start","data":{}}' '{"type":"assistant.message","data":{"messageId":"m-9","content":"DECISION NEEDED: unfinished turn"}}' > "$HOME/.copilot/session-state/$S2/events.jsonl"
expect "settle gives up when the turn has not ended" "$(settle $S2 "$(now_ms)" 2)" "0"
expect "and opens nothing for an unfinished turn" "$(q "select count(*) from hooks.decisions where session_id='$S2'")" "0"
printf '%s\n' '{"type":"assistant.turn_start","data":{}}' '{"type":"assistant.message","data":{"messageId":"m-8","content":"DECISION NEEDED: previous turn"}}' "{\"type\":\"assistant.turn_end\",\"data\":{},\"timestamp\":\"$(iso "$(now_ms)")\"}" '{"type":"user.message","data":{"content":"next"}}' > "$HOME/.copilot/session-state/$S2/events.jsonl"
settle $S2 "$(now_ms)" 2 > /dev/null
expect "a turn from before the latest prompt is not taken for this one" "$(q "select count(*) from hooks.decisions where session_id='$S2'")" "0"

turn $S2 "DECISION NEEDED: the previous turn, not flushed yet" msg-7 "$(( $(now_ms) - 60000 ))"
expect "a turn that ended before the stop is not taken for it" "$(settle $S2 "$(now_ms)" 2)" "0"
expect "so no decision is opened from it" "$(q "select count(*) from hooks.decisions where session_id='$S2'")" "0"
turn $S2 "DECISION NEEDED: this turn" msg-6 "$(( $(now_ms) - 300 ))"
settle $S2 "$(now_ms)" 2 > /dev/null
expect "a turn that ended just before the stop is" "$(q "select count(*) from hooks.decisions where session_id='$S2'")" "1"
psql -X -q -d "$DB" -c "delete from hooks.decisions where session_id='$S2'"

mkdir -p "$T/elsewhere"; printf '%s\n' '{"type":"assistant.message","data":{"content":"DECISION NEEDED: from outside"}}' > "$T/elsewhere/events.jsonl"
settle_outside() { PB_SETTLE_TRIES=1 "$BIN/pb-stop-settle" "$S2" "$1" "$(now_ms)" > /dev/null 2>&1; }
settle_outside "$T/elsewhere/events.jsonl"
expect "a transcript outside the session store is not read" "$(q "select count(*) from hooks.decisions where session_id='$S2'")" "0"
settle_outside "$HOME/.copilot/session-state/../../elsewhere/events.jsonl"
expect "a path with .. is not read" "$(q "select count(*) from hooks.decisions where session_id='$S2'")" "0"

expect "a payload without a session id is ignored" "$(hook sessionStart '{"cwd":"/x"}')" "0 0"
expect "malformed input is ignored" "$(hook sessionStart 'not json at all')" "0 0"
expect "an unknown event is ignored" "$(hook preToolUse "{\"sessionId\":\"$S1\"}")" "0 0"
expect "nothing extra was registered" "$(q "select count(*) from hooks.sessions")" "2"

S3=cccccccc-1111-2222-3333-000000000003
PB_HOOKS_DISABLE=1 hook sessionStart "{\"sessionId\":\"$S3\"}" > /dev/null
expect "the kill switch writes nothing" "$(q "select count(*) from hooks.sessions where session_id='$S3'")" "0"
touch "$HOME/.config/projectbuddy-hooks/disabled"
hook sessionStart "{\"sessionId\":\"$S3\"}" > /dev/null
expect "the disabled file writes nothing" "$(q "select count(*) from hooks.sessions where session_id='$S3'")" "0"
rm "$HOME/.config/projectbuddy-hooks/disabled"

sed "s/PGHOST=\"[^\"]*\"/PGHOST=\"10.255.255.1\"/" "$ENVFILE" > "$T/env-unreachable"
sed "s|PB_HOOKS_PASSWORD_FILE=\"[^\"]*\"|PB_HOOKS_PASSWORD_FILE=\"/nonexistent\"|" "$ENVFILE" > "$T/env-nopw"
start="$(date +%s.%N)"
res="$(PB_HOOKS_ENV="$T/env-unreachable" hook sessionStart "{\"sessionId\":\"$S3\"}")"
elapsed="$(awk -v a="$start" -v b="$(date +%s.%N)" 'BEGIN { print b - a }')"
expect "an unreachable database fails open" "$res" "0 0"
expect "and within the 2 s budget" "$(awk -v e="$elapsed" 'BEGIN { print (e < 2.0) ? 1 : 0 }')" "1"
expect "the unreachable-database run wrote nothing" "$(q "select count(*) from hooks.sessions where session_id='$S3'")" "0"
expect "the failure is logged" "$(grep -c 'database write failed' "$HOME/.local/state/projectbuddy-hooks/hooks.log")" "1"
res="$(PB_HOOKS_ENV="$T/env-nopw" hook sessionStart "{\"sessionId\":\"$S3\"}")"
expect "a missing password file fails open" "$res" "0 0"
expect "no secret is in the log" "$(grep -c "role-pw-$SUFFIX" "$HOME/.local/state/projectbuddy-hooks/hooks.log")" "0"

# --- KPI sync ---
cat > "$T/kpi" <<'KPI'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$KPI_LOG"
[ "${KPI_FAIL:-}" = 1 ] && exit 1
exit 0
KPI
chmod +x "$T/kpi"; : > "$T/kpi.log"
printf 'x\n' > "$T/kpipw"
cat >> "$ENVFILE" <<ENV
export PB_KPI_BIN="$T/kpi" PB_KPI_PASSWORD_FILE="$T/kpipw" PB_KPI_USER=postgres KPI_LOG="$T/kpi.log"
ENV
sync_run() { "$BIN/pb-kpi-sync" "$1" > /dev/null 2>&1; echo $?; }

psql -X -q -d "$DB" -c "update hooks.sessions set work_ref='VITechnologies/projectbuddy#381' where session_id='$S1'"
expect "kpi-sync exits 0" "$(sync_run $S1)" "0"
expect "kpi-sync ingests" "$(grep -c '^ingest ' "$T/kpi.log")" "1"
expect "kpi-sync marks with the registry's work_ref" "$(grep -c "^mark --session $S1 --item VITechnologies/projectbuddy#381 " "$T/kpi.log")" "1"
expect "kpi-sync resolves" "$(grep -c '^resolve ' "$T/kpi.log")" "1"
expect "the kpi password is passed as a file, not a value" "$(grep -c -- "--password-file $T/kpipw" "$T/kpi.log")" "3"
: > "$T/kpi.log"
sync_run $S1 > /dev/null
expect "a second run within a minute skips ingest and resolve" "$(grep -c -E '^(ingest|resolve) ' "$T/kpi.log")" "0"
expect "but still marks" "$(grep -c '^mark ' "$T/kpi.log")" "1"
: > "$T/kpi.log"
sync_run $S2 > /dev/null
expect "no work_ref, no mark" "$(grep -c '^mark ' "$T/kpi.log")" "0"
expect "kpi-sync with a failing kpi still exits 0" "$(KPI_FAIL=1 sync_run $S1)" "0"
before="$(wc -l < "$T/kpi.log" | tr -d ' ')"
expect "kpi-sync rejects a bad session id" "$(sync_run 'bad id;rm')" "0"
expect "and does nothing for it" "$(wc -l < "$T/kpi.log" | tr -d ' ')" "$before"

: > "$T/kpi.log"; rm -f "$HOME/.local/state/projectbuddy-hooks/kpi-sync.stamp"
hook sessionStart "{\"sessionId\":\"$S1\",\"cwd\":\"/work/x\"}" > /dev/null
for _ in $(seq 1 30); do grep -q '^mark ' "$T/kpi.log" && break; sleep 0.1; done
expect "sessionStart starts the kpi sync in the background" "$(grep -c '^mark ' "$T/kpi.log")" "1"

[ "$fails" != 0 ] && tail -8 "$HOME/.local/state/projectbuddy-hooks/hooks.log"
[ "$fails" = 0 ] && echo "ALL TESTS PASSED" || { echo "TESTS FAILED"; exit 1; }
