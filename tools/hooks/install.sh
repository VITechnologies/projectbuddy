#!/usr/bin/env bash
# Installs the workstation hooks (#381) in two steps, so that creating the files and the database role never changes a live session:
#
#   install.sh install --admin-password-file <file> [--prefix <dir>] [--host h] [--port n] [--database d] [--role r]
#                      [--kpi-bin <path>] [--kpi-password-file <file>]
#   install.sh activate [--prefix <dir>] [--hooks-dir <dir>]      # writes ~/.copilot/hooks/projectbuddy.json: every session now fires them
#   install.sh deactivate [--hooks-dir <dir>]                     # removes it again (the kill switch for one machine)
#
# `install` needs the hooks migration (0006_hooks.sql, applied by `projectbuddy-kpi migrate`) and a PostgreSQL superuser password
# file. It creates the login role (default pb_hooks, member of hooks_writer, no other rights) with a random password in a 0600 file,
# and is idempotent: an existing role and password file are kept.
set -euo pipefail

step="${1:-}"; shift || true
prefix="$HOME/.local/share/projectbuddy-hooks"
hooks_dir="$HOME/.copilot/hooks"
config_dir="$HOME/.config/projectbuddy-hooks"
host=127.0.0.1 port=55432 database=kpi_local role=pb_hooks admin_pw="" kpi_bin="" kpi_pw=""
while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) prefix="${2:?}"; shift 2 ;;
    --hooks-dir) hooks_dir="${2:?}"; shift 2 ;;
    --host) host="${2:?}"; shift 2 ;;
    --port) port="${2:?}"; shift 2 ;;
    --database) database="${2:?}"; shift 2 ;;
    --role) role="${2:?}"; shift 2 ;;
    --admin-password-file) admin_pw="${2:?}"; shift 2 ;;
    --kpi-bin) kpi_bin="${2:?}"; shift 2 ;;
    --kpi-password-file) kpi_pw="${2:?}"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
ident='^[a-z_][a-z0-9_]{0,62}$'
[[ "$role" =~ $ident && "$database" =~ $ident && "$port" =~ ^[0-9]+$ ]] || { echo "invalid --role, --database or --port" >&2; exit 2; }
here="$(cd "$(dirname "$0")" && pwd)"
umask 077

case "$step" in
  install)
    [ -r "$admin_pw" ] || { echo "--admin-password-file must be a readable file" >&2; exit 2; }
    mkdir -p "$prefix" "$config_dir"
    install -m 0755 "$here/scripts/pb-hook" "$here/scripts/pb-kpi-sync" "$here/scripts/pb-stop-settle" "$prefix/"
    admin() { PGPASSWORD="$(tr -d '\r\n' < "$admin_pw")" PGCONNECT_TIMEOUT=5 psql -X -At -w -h "$host" -p "$port" -U postgres -d "$database" -v ON_ERROR_STOP=1 "$@"; }
    [ "$(admin -c "select count(*) from pg_namespace where nspname = 'hooks'")" = 1 ] \
      || { echo "schema hooks is missing: run 'projectbuddy-kpi migrate' (0006_hooks.sql) on $database first" >&2; exit 1; }
    if [ ! -s "$config_dir/password" ]; then
      new_pw="$(head -c 24 /dev/urandom | base64 | tr -d '+/=\n')"
      # The password goes in on stdin, so it is never in a process listing; the file is written only once the role has it.
      if [ "$(admin -c "select count(*) from pg_roles where rolname = '$role'")" = 1 ]; then
        printf "alter role \"%s\" login password '%s'; grant hooks_writer to \"%s\";\n" "$role" "$new_pw" "$role" | admin > /dev/null
      else
        printf "create role \"%s\" login in role hooks_writer password '%s';\n" "$role" "$new_pw" | admin > /dev/null
      fi
      printf '%s' "$new_pw" > "$config_dir/password"
    fi
    {
      echo "export PB_HOOKS_PASSWORD_FILE=\"$config_dir/password\" PGHOST=\"$host\" PGPORT=\"$port\" PGDATABASE=\"$database\" PGUSER=\"$role\""
      [ -z "$kpi_bin" ] || echo "export PB_KPI_BIN=\"$kpi_bin\" PB_KPI_PASSWORD_FILE=\"$kpi_pw\""
    } > "$config_dir/env"
    echo "installed into $prefix; not active yet: run '$0 activate'" ;;
  activate)
    [ -x "$prefix/pb-hook" ] || { echo "run '$0 install' first" >&2; exit 1; }
    mkdir -p "$hooks_dir"
    python3 - "$prefix/pb-hook" "$hooks_dir/projectbuddy.json" <<'PY'
import json, sys
hook, target = sys.argv[1:]
entry = lambda event: [{"type": "command", "exec": hook, "args": [event], "timeoutSec": 2}]
config = {"version": 1, "hooks": {e: entry(e) for e in ("sessionStart", "userPromptSubmitted", "agentStop")}}
with open(target + ".tmp", "w") as handle:
    json.dump(config, handle, indent=2)
    handle.write("\n")
import os
os.replace(target + ".tmp", target)
PY
    echo "active: $hooks_dir/projectbuddy.json (applies to new sessions)" ;;
  deactivate)
    rm -f "$hooks_dir/projectbuddy.json"
    echo "deactivated" ;;
  *) echo "usage: $0 install|activate|deactivate (see the header)" >&2; exit 2 ;;
esac
