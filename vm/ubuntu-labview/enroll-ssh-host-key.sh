#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SSH_HOST="${SSH_HOST:-127.0.0.1}"
SSH_PORT="${SSH_PORT:-2222}"
SSH_KNOWN_HOSTS="${SSH_KNOWN_HOSTS:-}"
EXPECTED_SSH_HOST_FINGERPRINT="${EXPECTED_SSH_HOST_FINGERPRINT:-}"

if (($#)); then
    echo "Usage: EXPECTED_SSH_HOST_FINGERPRINT=<SHA256:...> SSH_KNOWN_HOSTS=<path> $0" >&2
    exit 2
fi
[[ -n "$EXPECTED_SSH_HOST_FINGERPRINT" ]] || {
    echo "SSH host-key enrollment: FAIL: EXPECTED_SSH_HOST_FINGERPRINT is required" >&2
    exit 2
}
[[ -n "$SSH_KNOWN_HOSTS" ]] || {
    echo "SSH host-key enrollment: FAIL: SSH_KNOWN_HOSTS is required" >&2
    exit 2
}

"$SCRIPT_DIR/enroll-ssh-host-key.py" \
    --host "$SSH_HOST" \
    --port "$SSH_PORT" \
    --expected-fingerprint "$EXPECTED_SSH_HOST_FINGERPRINT" \
    --out "$SSH_KNOWN_HOSTS"
if ! chmod 600 "$SSH_KNOWN_HOSTS"; then
    rm -f "$SSH_KNOWN_HOSTS"
    echo "SSH host-key enrollment: FAIL: could not secure SSH_KNOWN_HOSTS" >&2
    exit 2
fi