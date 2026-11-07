#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VM_NAME="${VM_NAME:-}"
EXPECTED_VM_UUID="${EXPECTED_VM_UUID:-}"
SSH_HOST="${SSH_HOST:-127.0.0.1}"
SSH_PORT="${SSH_PORT:-2222}"
GUEST_USER="${GUEST_USER:-actor}"
SNAPSHOT_NAME="${SNAPSHOT_NAME:-labview2026-q3-pro-activated-ready}"
EVIDENCE_DIR="${EVIDENCE_DIR:-}"
VIPM_ENV_FILE="${VIPM_ENV_FILE:-}"
GUEST_PASSWORD_FILE="${GUEST_PASSWORD_FILE:-}"
SSH_KNOWN_HOSTS="${SSH_KNOWN_HOSTS:-}"
evidence_files=(
    activation-known-answer-capture.json
    sandbox-runtime-state.json
    sandbox-vipm-about.json
    sandbox-vipm-labview.json
    sandbox-vipm-installed.json
)

usage() {
    cat <<'EOF'
Usage:
    EXPECTED_VM_UUID=<registered-vm-uuid> \
    VIPM_ENV_FILE=<mode-600-file> GUEST_PASSWORD_FILE=<mode-600-file> \
  SSH_KNOWN_HOSTS=<known-hosts-file> EVIDENCE_DIR=<nvme-directory> \
  ./complete-activation.sh

Optional: SSH_HOST, SSH_PORT, GUEST_USER. VM_NAME is required (no default --
this script is a generic engine shared by any fork).
No prior human-interactive LabVIEW activation is needed (confirmed via
real workflow_dispatch proof, #62): this NI Linux LabVIEW distribution
needs no separate license-activation dialog -- VIPM activation (run here
automatically) is the only activation this script performs or needs.
Before the first run against a given appliance, ensure the guest's Guest
Additions match the host's VirtualBox version and VIPM Desktop's
installed-package database has been initialized at least once (see
docs/vm-provisioning/procedure.md's "Interactive console access"
section) -- a stale appliance fails with a null VRDE framebuffer or a
LabViewVersionNotFound error respectively, not a licensing error.
EOF
}

if (($#)); then
    [[ "$1" == -h || "$1" == --help ]] && { usage; exit 0; }
    echo "activation completion: FAIL: unexpected arguments" >&2
    usage >&2
    exit 2
fi
for name in VIPM_ENV_FILE GUEST_PASSWORD_FILE SSH_KNOWN_HOSTS EVIDENCE_DIR; do
    [[ -n "${!name}" ]] || { echo "activation completion: FAIL: $name is required" >&2; exit 2; }
done
uuid_pattern='^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$'
[[ "$EXPECTED_VM_UUID" =~ $uuid_pattern ]] || {
    echo "activation completion: FAIL: EXPECTED_VM_UUID is required and must be a UUID" >&2
    exit 2
}
[[ "$VM_NAME" =~ ^[A-Za-z0-9._-]+$ ]] || {
    echo "activation completion: FAIL: VM_NAME is invalid" >&2
    exit 2
}
[[ "$GUEST_USER" =~ ^[a-z_][a-z0-9_-]*$ ]] || {
    echo "activation completion: FAIL: GUEST_USER is invalid" >&2
    exit 2
}
[[ "$SSH_HOST" == 127.0.0.1 ]] || {
    echo "activation completion: FAIL: SSH_HOST must be loopback 127.0.0.1" >&2
    exit 2
}
if [[ "$SSH_PORT" =~ ^[0-9]+$ ]]; then
    if ((10#$SSH_PORT < 1024 || 10#$SSH_PORT > 65535)); then
        echo "activation completion: FAIL: SSH_PORT must be from 1024 through 65535" >&2
        exit 2
    fi
else
    echo "activation completion: FAIL: SSH_PORT must be from 1024 through 65535" >&2
    exit 2
fi
[[ "$SNAPSHOT_NAME" == labview2026-q3-pro-activated-ready ]] || {
    echo "activation completion: FAIL: SNAPSHOT_NAME does not match the receipt contract" >&2
    exit 2
}
for path in "$VIPM_ENV_FILE" "$GUEST_PASSWORD_FILE" "$SSH_KNOWN_HOSTS" "$EVIDENCE_DIR"; do
    [[ "$path" == /* ]] || {
        echo "activation completion: FAIL: credential and evidence paths must be absolute" >&2
        exit 2
    }
done
for path in "$VIPM_ENV_FILE" "$GUEST_PASSWORD_FILE"; do
    [[ -f "$path" && "$(stat -c '%a' "$path")" == 600 ]] || {
        echo "activation completion: FAIL: secret files must exist with mode 600" >&2
        exit 2
    }
done
[[ -f "$SSH_KNOWN_HOSTS" ]] || { echo "activation completion: FAIL: SSH_KNOWN_HOSTS is missing" >&2; exit 2; }
[[ "$(stat -c '%a' "$SSH_KNOWN_HOSTS")" == 600 ]] || {
    echo "activation completion: FAIL: SSH_KNOWN_HOSTS must have mode 600" >&2
    exit 2
}
command -v VBoxManage >/dev/null 2>&1 || { echo "activation completion: FAIL: VBoxManage is missing" >&2; exit 127; }
evidence_parent="$(dirname "$EVIDENCE_DIR")"
while [[ ! -e "$evidence_parent" ]]; do evidence_parent="$(dirname "$evidence_parent")"; done
evidence_source="$(findmnt -no SOURCE -T "$evidence_parent")"
[[ "$evidence_source" == /dev/nvme* ]] || {
    echo "activation completion: FAIL: EVIDENCE_DIR must be backed by internal NVMe" >&2
    exit 2
}
install -d -m 700 "$EVIDENCE_DIR"
[[ "$(stat -c '%a' "$EVIDENCE_DIR")" == 700 ]] || {
    echo "activation completion: FAIL: EVIDENCE_DIR must enforce mode 700" >&2
    exit 2
}
for name in "${evidence_files[@]}" activation-receipt.json activation-receipt.json.sha256; do
    [[ ! -e "$EVIDENCE_DIR/$name" ]] || {
        echo "activation completion: FAIL: evidence output already exists: $name" >&2
        exit 2
    }
done

vm_info="$(VBoxManage showvminfo "$VM_NAME" --machinereadable)"
vm_uuid="$(sed -n 's/^UUID="\(.*\)"$/\1/p' <<<"$vm_info")"
vm_state="$(sed -n 's/^VMState="\(.*\)"$/\1/p' <<<"$vm_info")"
[[ -n "$vm_uuid" && "$vm_state" == running ]] || {
    echo "activation completion: FAIL: expected a running VM with a valid UUID" >&2
    exit 2
}
[[ "${vm_uuid,,}" == "${EXPECTED_VM_UUID,,}" ]] || {
    echo "activation completion: FAIL: VM UUID does not match expected identity" >&2
    exit 2
}
snapshot_inventory="$(VBoxManage snapshot "$VM_NAME" list --machinereadable)" || {
    echo "activation completion: FAIL: cannot inspect existing snapshots" >&2
    exit 2
}
if sed -n 's/^SnapshotName\(-[0-9][0-9]*\)\?="\(.*\)"$/\2/p' <<<"$snapshot_inventory" | grep -Fqx "$SNAPSHOT_NAME"; then
    echo "activation completion: FAIL: target snapshot already exists" >&2
    exit 2
fi

temporary_dir="$(mktemp -d)"
askpass="$temporary_dir/askpass.sh"
cleanup() { rm -rf "$temporary_dir"; }
trap cleanup EXIT
cat >"$askpass" <<'EOF'
#!/usr/bin/env bash
cat "$SANDBOX_PASSWORD_FILE"
EOF
chmod 700 "$askpass"
export DISPLAY=:0 SSH_ASKPASS="$askpass" SSH_ASKPASS_REQUIRE=force
export SANDBOX_PASSWORD_FILE="$GUEST_PASSWORD_FILE"
ssh_options=(-p "$SSH_PORT" -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$SSH_KNOWN_HOSTS")
scp_options=(-P "$SSH_PORT" -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$SSH_KNOWN_HOSTS")
remote="$GUEST_USER@$SSH_HOST"

setsid -w ssh "${ssh_options[@]}" "$remote" 'rm -rf /tmp/sandbox-activation && install -d -m 700 /tmp/sandbox-activation'
setsid -w scp "${scp_options[@]}" \
    "$SCRIPT_DIR/activate-vipm.sh" "$SCRIPT_DIR/capture-activation.sh" \
    "$remote:/tmp/sandbox-activation/"
setsid -w ssh "${ssh_options[@]}" "$remote" \
    'chmod 700 /tmp/sandbox-activation/activate-vipm.sh /tmp/sandbox-activation/capture-activation.sh; umask 077; cat > /tmp/sandbox-activation/vipm.env' \
    <"$VIPM_ENV_FILE"
setsid -w ssh "${ssh_options[@]}" "$remote" \
    '/tmp/sandbox-activation/capture-activation.sh --vipm-env /tmp/sandbox-activation/vipm.env --output-dir /tmp/sandbox-activation/evidence'

for name in "${evidence_files[@]}"; do
    setsid -w scp "${scp_options[@]}" "$remote:/tmp/sandbox-activation/evidence/$name" "$EVIDENCE_DIR/$name"
    chmod 600 "$EVIDENCE_DIR/$name"
    [[ "$(stat -c '%a' "$EVIDENCE_DIR/$name")" == 600 ]] || {
        echo "activation completion: FAIL: evidence files must enforce mode 600" >&2
        exit 2
    }
done
boot_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["bootId"])' "$EVIDENCE_DIR/sandbox-runtime-state.json")"
[[ -n "$boot_id" ]] || { echo "activation completion: FAIL: bootId is missing from sandbox-runtime-state.json" >&2; exit 2; }

{ cat "$GUEST_PASSWORD_FILE"; printf '\n'; } | setsid -w ssh "${ssh_options[@]}" "$remote" \
    'rm -rf /tmp/sandbox-activation && sudo -S -p "" systemctl poweroff'
for _ in $(seq 1 180); do
    vm_state="$(VBoxManage showvminfo "$VM_NAME" --machinereadable | sed -n 's/^VMState="\(.*\)"$/\1/p')"
    [[ "$vm_state" == poweroff ]] && break
    sleep 1
done
[[ "$vm_state" == poweroff ]] || { echo "activation completion: FAIL: guest did not power off cleanly" >&2; exit 1; }

VBoxManage snapshot "$VM_NAME" take "$SNAPSHOT_NAME" \
    --description 'LabVIEW 2026 Q3 Professional and VIPM 26.3.2 Professional functionally verified; autologin disabled; evidence stored separately on NVMe.'
snapshot_uuid="$(VBoxManage showvminfo "$VM_NAME" --machinereadable | sed -n 's/^CurrentSnapshotUUID="\(.*\)"$/\1/p')"
[[ -n "$snapshot_uuid" ]] || { echo "activation completion: FAIL: snapshot UUID is missing" >&2; exit 1; }

"$SCRIPT_DIR/finalize-activation-receipt.py" \
    --evidence-dir "$EVIDENCE_DIR" \
    --vm-name "$VM_NAME" --vm-uuid "$vm_uuid" --snapshot-name "$SNAPSHOT_NAME" \
    --snapshot-uuid "$snapshot_uuid"
echo "activation completion: PASS: snapshot $SNAPSHOT_NAME ($snapshot_uuid)"
echo "activation completion: PASS: receipt $EVIDENCE_DIR/activation-receipt.json"
