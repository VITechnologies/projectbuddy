#!/usr/bin/env bash
set -euo pipefail

# complete-runner-registration.sh: host-side orchestrator for the Ubuntu
# family's `register-runner` phase -- copies install-actions-runner.sh onto
# the guest over SSH and invokes it as root (sudo -S, password piped via
# stdin exactly like complete-activation.sh's own poweroff step; this VM
# family has no passwordless sudo configured). No new VirtualBox snapshot
# is taken and no NVMe evidence directory is required: unlike
# complete-activation (a one-time, expensive-to-redo milestone), runner
# registration is a safe, idempotent, re-runnable operation -- a
# registered, online runner with an unlicensed LabVIEW/VIPM install is a
# safe, inert state (same principle the Windows family's register-runner
# phase already documents).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VM_NAME="${VM_NAME:-}"
EXPECTED_VM_UUID="${EXPECTED_VM_UUID:-}"
SSH_HOST="${SSH_HOST:-127.0.0.1}"
SSH_PORT="${SSH_PORT:-2222}"
GUEST_USER="${GUEST_USER:-actor}"
GUEST_PASSWORD_FILE="${GUEST_PASSWORD_FILE:-}"
SSH_KNOWN_HOSTS="${SSH_KNOWN_HOSTS:-}"
PAT_FILE="${PAT_FILE:-}"
TARGET_REPO="${TARGET_REPO:-}"
RUNNER_NAME="${RUNNER_NAME:-}"
RUNNER_LABELS="${RUNNER_LABELS:-}"
SERVICE_ACCOUNT="${SERVICE_ACCOUNT:-}"
RUNNER_URL="${RUNNER_URL:-}"
RUNNER_FILENAME="${RUNNER_FILENAME:-}"
RUNNER_SHA256="${RUNNER_SHA256:-}"

usage() {
    cat <<'EOF'
Usage:
  EXPECTED_VM_UUID=<registered-vm-uuid> GUEST_PASSWORD_FILE=<mode-600 file> \
  SSH_KNOWN_HOSTS=<known-hosts-file> PAT_FILE=<mode-600 file> \
  TARGET_REPO=<owner/repo> RUNNER_NAME=<name> RUNNER_LABELS=<csv> \
  SERVICE_ACCOUNT=<name> RUNNER_URL=<url> RUNNER_FILENAME=<file> \
  RUNNER_SHA256=<sha256> ./complete-runner-registration.sh

Optional: SSH_HOST, SSH_PORT, GUEST_USER. VM_NAME is required (no default --
this script is a generic engine shared by any fork).
EOF
}

if (($#)); then
    [[ "$1" == -h || "$1" == --help ]] && { usage; exit 0; }
    echo "runner registration: FAIL: unexpected arguments" >&2
    usage >&2
    exit 2
fi
for name in VM_NAME GUEST_PASSWORD_FILE SSH_KNOWN_HOSTS PAT_FILE TARGET_REPO RUNNER_NAME RUNNER_LABELS SERVICE_ACCOUNT RUNNER_URL RUNNER_FILENAME RUNNER_SHA256; do
    [[ -n "${!name}" ]] || { echo "runner registration: FAIL: $name is required" >&2; exit 2; }
done
uuid_pattern='^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$'
[[ "$EXPECTED_VM_UUID" =~ $uuid_pattern ]] || {
    echo "runner registration: FAIL: EXPECTED_VM_UUID is required and must be a UUID" >&2
    exit 2
}
[[ "$VM_NAME" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "runner registration: FAIL: VM_NAME is invalid" >&2; exit 2; }
[[ "$GUEST_USER" =~ ^[a-z_][a-z0-9_-]*$ ]] || { echo "runner registration: FAIL: GUEST_USER is invalid" >&2; exit 2; }
[[ "$SSH_HOST" == 127.0.0.1 ]] || { echo "runner registration: FAIL: SSH_HOST must be loopback 127.0.0.1" >&2; exit 2; }
if [[ "$SSH_PORT" =~ ^[0-9]+$ ]]; then
    if ((10#$SSH_PORT < 1024 || 10#$SSH_PORT > 65535)); then
        echo "runner registration: FAIL: SSH_PORT must be from 1024 through 65535" >&2
        exit 2
    fi
else
    echo "runner registration: FAIL: SSH_PORT must be from 1024 through 65535" >&2
    exit 2
fi
for path in "$GUEST_PASSWORD_FILE" "$SSH_KNOWN_HOSTS" "$PAT_FILE"; do
    [[ "$path" == /* ]] || { echo "runner registration: FAIL: credential paths must be absolute" >&2; exit 2; }
done
for path in "$GUEST_PASSWORD_FILE" "$PAT_FILE"; do
    [[ -f "$path" && "$(stat -c '%a' "$path")" == 600 ]] || {
        echo "runner registration: FAIL: secret files must exist with mode 600" >&2
        exit 2
    }
done
[[ -f "$SSH_KNOWN_HOSTS" && "$(stat -c '%a' "$SSH_KNOWN_HOSTS")" == 600 ]] || {
    echo "runner registration: FAIL: SSH_KNOWN_HOSTS is missing or not mode 600" >&2
    exit 2
}
command -v VBoxManage >/dev/null 2>&1 || { echo "runner registration: FAIL: VBoxManage is missing" >&2; exit 127; }

vm_info="$(VBoxManage showvminfo "$VM_NAME" --machinereadable)"
vm_uuid="$(sed -n 's/^UUID="\(.*\)"$/\1/p' <<<"$vm_info")"
vm_state="$(sed -n 's/^VMState="\(.*\)"$/\1/p' <<<"$vm_info")"
[[ -n "$vm_uuid" && "$vm_state" == running ]] || {
    echo "runner registration: FAIL: expected a running VM with a valid UUID" >&2
    exit 2
}
[[ "${vm_uuid,,}" == "${EXPECTED_VM_UUID,,}" ]] || {
    echo "runner registration: FAIL: VM UUID does not match expected identity" >&2
    exit 2
}

ssh_options=(-p "$SSH_PORT" -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$SSH_KNOWN_HOSTS")
scp_options=(-P "$SSH_PORT" -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$SSH_KNOWN_HOSTS")
remote="$GUEST_USER@$SSH_HOST"

setsid -w ssh "${ssh_options[@]}" "$remote" 'rm -rf /tmp/sandbox-runner-registration && install -d -m 700 /tmp/sandbox-runner-registration'
setsid -w scp "${scp_options[@]}" \
    "$SCRIPT_DIR/install-actions-runner.sh" \
    "$remote:/tmp/sandbox-runner-registration/"
setsid -w ssh "${ssh_options[@]}" "$remote" \
    'chmod 700 /tmp/sandbox-runner-registration/install-actions-runner.sh; umask 077; cat > /tmp/sandbox-runner-registration/runner.pat' \
    <"$PAT_FILE"
setsid -w ssh "${ssh_options[@]}" "$remote" 'chmod 600 /tmp/sandbox-runner-registration/runner.pat'

remote_command=$(printf 'sudo -S -p "" /tmp/sandbox-runner-registration/install-actions-runner.sh \
    --repo %q --runner-name %q --labels %q --service-account %q \
    --runner-url %q --runner-filename %q --runner-sha256 %q \
    --pat-file /tmp/sandbox-runner-registration/runner.pat' \
    "$TARGET_REPO" "$RUNNER_NAME" "$RUNNER_LABELS" "$SERVICE_ACCOUNT" \
    "$RUNNER_URL" "$RUNNER_FILENAME" "$RUNNER_SHA256")
registration_exit=0
{ cat "$GUEST_PASSWORD_FILE"; printf '\n'; } | setsid -w ssh "${ssh_options[@]}" "$remote" "$remote_command" || registration_exit=$?

setsid -w ssh "${ssh_options[@]}" "$remote" 'rm -rf /tmp/sandbox-runner-registration' || true

if ((registration_exit != 0)); then
    echo "runner registration: FAIL: install-actions-runner.sh exited with code $registration_exit" >&2
    exit "$registration_exit"
fi
echo "runner registration: PASS: $RUNNER_NAME registered against $TARGET_REPO"
