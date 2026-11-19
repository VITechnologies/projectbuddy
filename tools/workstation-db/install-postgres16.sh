#!/usr/bin/env bash
# Root steps for the native PostgreSQL 16 workstation database (VITechnologies/projectbuddy#381).
# Idempotent: every step checks before it acts. Run with sudo. It never touches the Docker container
# rsr-registry-local-postgres or its volume, and never binds 55432 (the cutover is a later, separate step).
#
#   sudo tools/workstation-db/install-postgres16.sh [--staging-port 55433]
#
# What it does:
#   1. Refuses to run unless Data1 is a real mount point.
#   2. Adds the PGDG apt repo (Ubuntu ships PostgreSQL 18) and installs postgresql-16 without an auto "main" cluster.
#   3. Lets the postgres user traverse (execute-only ACL) the Data1 mount point.
#   4. Creates cluster 16/workstation with its data directory on Data1, bound to 127.0.0.1:<staging-port> only.
#   5. Adds a systemd drop-in with RequiresMountsFor so the service never starts before Data1 is mounted.
#   6. Sets the postgres role's password from the existing superuser password file (read as root, never printed) and
#      requires scram-sha-256 on 127.0.0.1, so the agent can restore without sudo.
set -euo pipefail

STAGING_PORT=55433
while [ $# -gt 0 ]; do
  case "$1" in
    --staging-port) STAGING_PORT="${2:?port}"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

MOUNT=/run/media/sergio/Data1
DATADIR="$MOUNT/postgresql/16/workstation"
CLUSTER=workstation
VERSION=16
OWNER="${SUDO_USER:-sergio}"
OWNER_HOME="$(getent passwd "$OWNER" | cut -d: -f6)"
PASSWORD_FILE="$OWNER_HOME/.config/rsr-registry-local/postgres-password"

log() { printf '[install-postgres16] %s\n' "$*"; }
[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }
[ "$STAGING_PORT" != 55432 ] || { echo "55432 is reserved for the cutover" >&2; exit 1; }
[[ "$STAGING_PORT" =~ ^[0-9]+$ ]] || { echo "invalid port" >&2; exit 1; }
mountpoint -q "$MOUNT" || { echo "$MOUNT is not mounted; refusing to put data on /" >&2; exit 1; }
[ -s "$PASSWORD_FILE" ] || { echo "missing $PASSWORD_FILE" >&2; exit 1; }
if ss -ltn "sport = :$STAGING_PORT" | grep -q LISTEN && ! pg_lsclusters -h 2>/dev/null | awk -v c="$CLUSTER" -v p="$STAGING_PORT" '$1==16 && $2==c && $3==p {f=1} END{exit !f}'; then
  echo "port $STAGING_PORT is in use by something else" >&2; exit 1
fi

export DEBIAN_FRONTEND=noninteractive

if ! dpkg -s postgresql-16 >/dev/null 2>&1; then
  log "installing PGDG repo and postgresql-16"
  apt-get update -qq
  apt-get install -y -qq postgresql-common acl
  /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y
  mkdir -p /etc/postgresql-common
  touch /etc/postgresql-common/createcluster.conf
  if grep -q '^#\?create_main_cluster' /etc/postgresql-common/createcluster.conf; then
    sed -i 's/^#\?create_main_cluster.*/create_main_cluster = false/' /etc/postgresql-common/createcluster.conf
  else
    echo 'create_main_cluster = false' >> /etc/postgresql-common/createcluster.conf
  fi
  apt-get install -y -qq postgresql-16 postgresql-client-16
else
  log "postgresql-16 already installed"
fi
command -v setfacl >/dev/null || apt-get install -y -qq acl

# Data1 is drwxrws--- ollama:sergio; postgres needs traverse (x) only, not read or list.
setfacl -m u:postgres:--x "$MOUNT"

if ! pg_lsclusters -h | awk -v c="$CLUSTER" '$1==16 && $2==c {f=1} END{exit !f}'; then
  log "creating cluster $VERSION/$CLUSTER at $DATADIR on port $STAGING_PORT"
  install -d -o postgres -g postgres -m 0700 "$MOUNT/postgresql" "$MOUNT/postgresql/16"
  [ ! -e "$DATADIR" ] || [ -z "$(ls -A "$DATADIR")" ] || { echo "$DATADIR exists and is not empty" >&2; exit 1; }
  pg_createcluster "$VERSION" "$CLUSTER" --datadir="$DATADIR" --port="$STAGING_PORT" --start-conf=auto \
    -- --auth-local=peer --auth-host=scram-sha-256 --encoding=UTF8
else
  log "cluster $VERSION/$CLUSTER already exists"
fi

pg_conftool "$VERSION" "$CLUSTER" set listen_addresses 127.0.0.1
pg_conftool "$VERSION" "$CLUSTER" set password_encryption scram-sha-256

UNIT="postgresql@$VERSION-$CLUSTER.service"
install -d /etc/systemd/system/"$UNIT".d
DROPIN=/etc/systemd/system/"$UNIT".d/data1.conf
printf '[Unit]\nRequiresMountsFor=%s\n' "$MOUNT" > "$DROPIN.tmp"
if ! cmp -s "$DROPIN.tmp" "$DROPIN" 2>/dev/null; then mv "$DROPIN.tmp" "$DROPIN"; else rm -f "$DROPIN.tmp"; fi
systemctl daemon-reload
systemctl enable "$UNIT" >/dev/null 2>&1 || true
systemctl start "$UNIT"

for _ in $(seq 1 30); do pg_isready -q -h 127.0.0.1 -p "$STAGING_PORT" && break; sleep 1; done
pg_isready -h 127.0.0.1 -p "$STAGING_PORT" >/dev/null || { echo "cluster did not become ready" >&2; exit 1; }

# The password travels over stdin as a quoted literal; it is never in argv or the log.
log "setting the postgres role password from the existing password file"
{ printf "ALTER ROLE postgres PASSWORD '"; tr -d '\r\n' < "$PASSWORD_FILE" | sed "s/'/''/g"; printf "';\n"; } \
  | runuser -u postgres -- psql -X -q -p "$STAGING_PORT" -v ON_ERROR_STOP=1 -d postgres >/dev/null

log "done: PostgreSQL $(runuser -u postgres -- psql -X -Atq -p "$STAGING_PORT" -d postgres -c 'show server_version') on 127.0.0.1:$STAGING_PORT, data in $DATADIR"
pg_lsclusters
