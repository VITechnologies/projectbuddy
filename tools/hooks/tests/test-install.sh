#!/usr/bin/env bash
# install.sh against a real PostgreSQL with a fake HOME: install creates files and a role but no hooks file; activate and
# deactivate toggle exactly one JSON file; re-running keeps the password.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
: "${PBK_TEST_PASSWORD_FILE:?set PBK_TEST_PASSWORD_FILE (and PBK_TEST_HOST/PORT/USER)}"
export PGHOST="${PBK_TEST_HOST:-127.0.0.1}" PGPORT="${PBK_TEST_PORT:-5432}"
PGPASSWORD="$(tr -d '\r\n' < "$PBK_TEST_PASSWORD_FILE")"; export PGPASSWORD
SUFFIX="$(head -c 6 /dev/urandom | od -An -tx1 | tr -d ' \n')"
DB="hooks_inst_$SUFFIX" ROLE="pb_inst_$SUFFIX"
T="$(mktemp -d)"
cleanup() { psql -X -q -U postgres -d postgres -c "drop database if exists \"$DB\" with (force)" -c "drop role if exists \"$ROLE\"" > /dev/null 2>&1; rm -rf "$T"; }
trap cleanup EXIT
fails=0
expect() { if [ "$2" = "$3" ]; then echo "ok   $1"; else echo "FAIL $1 (expected '$3', got '$2')"; fails=1; fi; }

psql -X -q -U postgres -d postgres -c "create database \"$DB\"" || exit 1
psql -X -q -U postgres -v ON_ERROR_STOP=1 -d "$DB" -f "$HERE/../../kpi/ProjectBuddy.Kpi/Sql/0006_hooks.sql" || exit 1
export HOME="$T/home"; mkdir -p "$HOME"
inst() { "$HERE/../install.sh" "$@" --prefix "$T/prefix" --hooks-dir "$T/hooks"; }
common=(--admin-password-file "$PBK_TEST_PASSWORD_FILE" --host "$PGHOST" --port "$PGPORT" --database "$DB" --role "$ROLE")

expect "activate before install is refused" "$(inst activate > /dev/null 2>&1; echo $?)" "1"
expect "install succeeds" "$(inst install "${common[@]}" > /dev/null; echo $?)" "0"
expect "install does not activate" "$(ls "$T/hooks" 2> /dev/null | wc -l | tr -d ' ')" "0"
expect "the password file is 0600" "$(stat -c %a "$HOME/.config/projectbuddy-hooks/password")" "600"
expect "the env file is 0600" "$(stat -c %a "$HOME/.config/projectbuddy-hooks/env")" "600"
expect "the scripts are installed" "$(ls "$T/prefix" | tr '\n' ' ')" "pb-hook pb-kpi-sync pb-stop-settle "
expect "the role is a member of hooks_writer only" "$(psql -X -At -U postgres -d postgres -c "select string_agg(g.rolname, ',') from pg_auth_members m join pg_roles g on g.oid=m.roleid join pg_roles r on r.oid=m.member where r.rolname='$ROLE'")" "hooks_writer"
before="$(sha256sum "$HOME/.config/projectbuddy-hooks/password")"
expect "install is idempotent" "$(inst install "${common[@]}" > /dev/null; echo $?)" "0"
expect "and keeps the password" "$(sha256sum "$HOME/.config/projectbuddy-hooks/password")" "$before"
expect "the installed role can log in with that password" "$(PGPASSWORD="$(cat "$HOME/.config/projectbuddy-hooks/password")" psql -X -At -U "$ROLE" -d "$DB" -c "select hooks.work_ref_of('abcdefgh1')" | wc -l | tr -d ' ')" "1"

expect "activate succeeds" "$(inst activate > /dev/null; echo $?)" "0"
expect "activate writes one hooks file" "$(ls "$T/hooks")" "projectbuddy.json"
expect "it lists exactly the three events, each with a 2 s timeout" "$(python3 -c "import json;c=json.load(open('$T/hooks/projectbuddy.json'));print(sorted(c['hooks']), {e[0]['timeoutSec'] for e in c['hooks'].values()}, c['hooks']['agentStop'][0]['exec'])")" "['agentStop', 'sessionStart', 'userPromptSubmitted'] {2} $T/prefix/pb-hook"
expect "no tool-use events are hooked" "$(grep -c -i 'ToolUse' "$T/hooks/projectbuddy.json")" "0"
expect "deactivate removes it" "$(inst deactivate > /dev/null; ls "$T/hooks" | wc -l | tr -d ' ')" "0"

psql -X -q -U postgres -d postgres -c "drop owned by \"$ROLE\"" > /dev/null
rm -rf "$HOME/.config/projectbuddy-hooks"; psql -X -q -U postgres -d "$DB" -c "drop schema hooks cascade" > /dev/null
expect "install without the migration is refused" "$(inst install "${common[@]}" > /dev/null 2>&1; echo $?)" "1"
[ "$fails" = 0 ] && echo "ALL TESTS PASSED" || { echo "TESTS FAILED"; exit 1; }
