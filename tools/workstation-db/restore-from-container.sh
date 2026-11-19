#!/usr/bin/env bash
# Copies roles and databases from the Docker container rsr-registry-local-postgres (source, read-only) into the native
# cluster (target), then verifies them (#381). Runs as the normal user; the container is never modified.
#
#   tools/workstation-db/restore-from-container.sh [--target-port 55433] [--wipe-target] [--verify-only]
#
# Re-runnable: with --wipe-target it drops the restored (non-system) databases and non-superuser roles first. Dumps live in a
# 0700 directory under /tmp (tmpfs) that is deleted on exit. Passwords come from the 0600 password file, never argv.
set -euo pipefail

SRC_PORT=55432
TARGET_PORT=55433
WIPE=0
VERIFY_ONLY=0
STATE_FILE="${PB_RESTORE_STATE:-$HOME/.local/state/projectbuddy-workstation-db/restore-state}"
while [ $# -gt 0 ]; do
  case "$1" in
    --target-port) TARGET_PORT="${2:?port}"; shift 2 ;;
    --wipe-target) WIPE=1; shift ;;
    --verify-only) VERIFY_ONLY=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ "$TARGET_PORT" != "$SRC_PORT" ] || { echo "target and source ports must differ" >&2; exit 1; }

BIN=/usr/lib/postgresql/16/bin
export PGHOST=127.0.0.1 PGUSER=postgres
PGPASSWORD="$(tr -d '\r\n' < "$HOME/.config/rsr-registry-local/postgres-password")"
export PGPASSWORD
WORK="$(mktemp -d /tmp/pb-restore.XXXXXX)"; chmod 700 "$WORK"
trap 'rm -rf "$WORK"' EXIT
umask 077

src() { "$BIN/psql" -X -Atq -p "$SRC_PORT" "$@"; }
tgt() { "$BIN/psql" -X -Atq -p "$TARGET_PORT" "$@"; }

mapfile -t DBS < <(src -d postgres -c "select datname from pg_database where not datistemplate and datname <> 'postgres' order by 1")
echo "databases: ${DBS[*]}"

counters() { src -d postgres -c "select datname||'|'||(tup_inserted+tup_updated+tup_deleted) from pg_stat_database where datname = any(string_to_array('$(IFS=,; echo "${DBS[*]}")', ',')) order by datname"; }
# Taken before the first dump: a write after this point (even during the verification) makes cutover.sh refuse.
sleep 2   # let the statistics collector flush writes that just happened
PRE_COUNTERS="$(counters)"
[ "$(printf '%s\n' "$PRE_COUNTERS" | grep -c '|')" = "${#DBS[@]}" ] || { echo "could not read write counters for every database" >&2; exit 1; }

if [ "$VERIFY_ONLY" = 0 ] && [ "$WIPE" = 1 ]; then
  for d in "${DBS[@]}"; do tgt -d postgres -c "drop database if exists \"$d\" with (force)"; done
  mapfile -t OLD < <(tgt -d postgres -c "select rolname from pg_roles where not rolsuper and rolname !~ '^pg_'")
  for r in "${OLD[@]}"; do tgt -d postgres -c "drop owned by \"$r\"; drop role \"$r\"" || true; done
fi

if [ "$VERIFY_ONLY" = 0 ]; then
  "$BIN/pg_dumpall" -p "$SRC_PORT" --roles-only > "$WORK/roles.sql"
  # The target already has the postgres superuser; that one "already exists" error is expected and ignored.
  "$BIN/psql" -X -q -p "$TARGET_PORT" -d postgres -f "$WORK/roles.sql" > /dev/null 2> "$WORK/roles.err" || true
  if grep -v 'role "postgres" already exists' "$WORK/roles.err" | grep -q ERROR; then
    grep ERROR "$WORK/roles.err" | grep -v 'role "postgres"' >&2; echo "unexpected role restore errors" >&2; exit 1
  fi

  for d in "${DBS[@]}"; do
    "$BIN/pg_dump" -p "$SRC_PORT" -Fc --create -f "$WORK/$d.dump" "$d"
    "$BIN/pg_restore" -p "$TARGET_PORT" -d postgres --create --exit-on-error "$WORK/$d.dump"
  done

  # Maintenance databases are not dumped; apply the same PUBLIC CONNECT/TEMPORARY revoke as the source's hardening (#15).
  tgt -d postgres -c "revoke connect, temporary on database postgres, template1 from public" 2>/dev/null \
    || { tgt -d postgres -c 'revoke connect, temporary on database postgres from public'; tgt -d postgres -c 'revoke connect, temporary on database template1 from public'; }
fi

fail=0
# Both sides are captured by plain assignments, so a failed query aborts the run instead of comparing two empty results.
cmpq() { local a b; a="$(src -v ON_ERROR_STOP=1 -d "$1" -c "$2")"; b="$(tgt -v ON_ERROR_STOP=1 -d "$1" -c "$2")"; check "$a" "$b" "$3"; }
check() { if [ "$1" = "$2" ]; then echo "ok   $3"; else echo "FAIL $3"; echo "  source: $1" | head -c 600; echo; echo "  target: $2" | head -c 600; echo; fail=1; fi; }

q_roles="select rolname, rolsuper, rolinherit, rolcreaterole, rolcreatedb, rolcanlogin, rolreplication, rolbypassrls, rolconnlimit, rolvaliduntil is null, rolpassword_set from (select r.*, (rolpassword is not null) as rolpassword_set from pg_authid r) r where rolname !~ '^pg_' order by 1"
cmpq postgres "$q_roles" "roles and attributes"
q_pw="select rolname, md5(coalesce(rolpassword,'')) from pg_authid where rolname !~ '^pg_' order by 1"
cmpq postgres "$q_pw" "role password verifiers"
q_member="select r.rolname, m.rolname, a.admin_option from pg_auth_members a join pg_roles r on r.oid=a.roleid join pg_roles m on m.oid=a.member where r.rolname !~ '^pg_' order by 1,2"
cmpq postgres "$q_member" "role memberships"
q_dbacl="select datname, datdba::regrole, datacl::text, datallowconn, datconnlimit from pg_database where not datistemplate or datname='template1' order by 1"
cmpq postgres "$q_dbacl" "database owners and ACLs (PUBLIC CONNECT/TEMPORARY hardening)"
q_set="select coalesce(datname,''), coalesce(rolname,''), setconfig::text from pg_db_role_setting s left join pg_database d on d.oid=s.setdatabase left join pg_roles r on r.oid=s.setrole order by 1,2"
cmpq postgres "$q_set" "per-database and per-role settings"

# An explicit owner-only ACL equals the default one, and pg_dump rightly omits it, so compare against acldefault.
q_acl="select 'S', nspname, coalesce(nspacl, acldefault('n'::\"char\", nspowner))::text from pg_namespace where nspname !~ '^pg_|information_schema' union all select 'R', n.nspname||'.'||c.relname, coalesce(c.relacl, acldefault(case c.relkind when 'S' then 's'::\"char\" else 'r'::\"char\" end, c.relowner))::text from pg_class c join pg_namespace n on n.oid=c.relnamespace where n.nspname !~ '^pg_|information_schema' and c.relkind in ('r','v','m','S','p') union all select 'F', n.nspname||'.'||p.oid::regprocedure::text, coalesce(p.proacl, acldefault('f'::\"char\", p.proowner))::text from pg_proc p join pg_namespace n on n.oid=p.pronamespace where n.nspname !~ '^pg_|information_schema' order by 1,2"
for d in "${DBS[@]}"; do
  cmpq "$d" "$q_acl" "$d: schema/table/function grants"
  rows="select n.nspname||'.'||c.relname from pg_class c join pg_namespace n on n.oid=c.relnamespace where c.relkind in ('r','p') and n.nspname !~ '^pg_|information_schema' order by 1"
  q_counts=""
  while IFS= read -r t; do
    q_counts+="${q_counts:+ union all }select '$t', count(*) from $(printf '%s' "$t" | sed 's/\([^.]*\)\.\(.*\)/"\1"."\2"/')"
  done < <(src -d "$d" -c "$rows")
  if [ -n "$q_counts" ]; then
    cmpq "$d" "select * from ($q_counts) x order by 1" "$d: exact row counts per table"
  fi
  q_obj="select count(*) from pg_class c join pg_namespace n on n.oid=c.relnamespace where n.nspname !~ '^pg_|information_schema'"
  cmpq "$d" "$q_obj" "$d: relation count (tables, indexes, views, sequences)"
done

if tgt -d postgres -c "select 1 from pg_roles where rolname='farm'" | grep -q 1; then echo "ok   role farm present"; else echo "FAIL role farm missing"; fail=1; fi
pub="$(tgt -d postgres -c "select string_agg(datname, ',') from pg_database where has_database_privilege('public', oid, 'CONNECT') and datname = any(array['kpi_local','registry_local','farm_local'])")"
[ -z "$pub" ] && echo "ok   PUBLIC cannot CONNECT to kpi_local, registry_local or farm_local on target" || { echo "FAIL PUBLIC can connect: $pub"; fail=1; }

if [ "$fail" != 0 ]; then echo "CHECKS FAILED"; exit 1; fi
echo "ALL CHECKS PASSED"

# cutover.sh refuses unless the source's write counters still match these (no writes since this verified copy).
if [ "$VERIFY_ONLY" = 0 ]; then
  mkdir -p "$(dirname "$STATE_FILE")"
  {
    echo "epoch=$(date +%s)"
    echo "$PRE_COUNTERS"
  } > "$STATE_FILE.tmp"
  mv "$STATE_FILE.tmp" "$STATE_FILE"
  echo "recorded source write counters in $STATE_FILE"
fi
