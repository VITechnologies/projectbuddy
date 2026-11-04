#!/usr/bin/env bash
set -euo pipefail

VM_NAME='@@VM_NAME@@'
VM_UUID='@@VM_UUID@@'
RECEIPT_DIR=/var/lib/sandbox-sbom-reporting
STATE="$RECEIPT_DIR/base-bootstrap-state.json"
RECEIPT="$RECEIPT_DIR/base-bootstrap-receipt.json"
FINALIZER=/usr/local/sbin/sandbox-base-bootstrap-finalize
SERVICE=/etc/systemd/system/sandbox-base-bootstrap-receipt.service
TIMER=/etc/systemd/system/sandbox-base-bootstrap-receipt.timer

[[ -n "$RECEIPT" ]] || { echo "base bootstrap: FAIL: receipt path was not initialized" >&2; exit 1; }
[[ $(id -u) -eq 0 ]] || { echo "base bootstrap must run as root" >&2; exit 1; }
[[ "$VM_NAME" != *'@@'* && "$VM_UUID" != *'@@'* ]] || { echo "VM identity placeholders were not replaced" >&2; exit 1; }
# shellcheck source=/etc/os-release disable=SC1091
source /etc/os-release
[[ "${ID:-}" == ubuntu && "${VERSION_ID:-}" == 24.04 && "${VERSION_CODENAME:-}" == noble ]] || {
  echo "base bootstrap requires Ubuntu 24.04 noble" >&2
  exit 1
}
case "$(cat /sys/class/dmi/id/sys_vendor 2>/dev/null || true)" in
  *Oracle*|*innotek*) ;;
  *) echo "VirtualBox guest hardware was not detected" >&2; exit 1 ;;
esac

export DEBIAN_FRONTEND=noninteractive
sed -i '\|^[[:space:]]*deb .*file:/\+cdrom|d' /etc/apt/sources.list
python3 - <<'PY'
import re
from pathlib import Path

for source in Path("/etc/apt/sources.list.d").glob("*.sources"):
    paragraphs = source.read_text(encoding="utf-8").split("\n\n")
    retained = [paragraph for paragraph in paragraphs if not any(
        line.lstrip().startswith("URIs:") and re.search(r"file:/+cdrom(?:/|\s|$)", line)
        for line in paragraph.splitlines()
    )]
    source.write_text("\n\n".join(retained).rstrip() + "\n", encoding="utf-8")
PY
apt-get update
apt-get install -y --no-install-recommends openssh-server git python3 virtualbox-guest-utils
systemctl enable ssh.service virtualbox-guest-utils.service

install -d -m 0755 "$RECEIPT_DIR"
python3 - "$STATE" "$VM_NAME" "$VM_UUID" <<'PY'
import json
import sys
from datetime import datetime, timezone

destination, vm_name, vm_uuid = sys.argv[1:]
with open(destination, "w", encoding="utf-8") as handle:
    json.dump(
        {
            "vm": {"name": vm_name, "uuid": vm_uuid},
            "os": {"distribution": "ubuntu", "version": "24.04", "codename": "noble"},
            "installedAt": datetime.now(timezone.utc).isoformat(),
        },
        handle,
        indent=2,
        sort_keys=True,
    )
    handle.write("\n")
PY

cat > "$FINALIZER" <<'FINALIZER'
#!/usr/bin/env bash
set -euo pipefail

STATE=/var/lib/sandbox-sbom-reporting/base-bootstrap-state.json
RECEIPT=/var/lib/sandbox-sbom-reporting/base-bootstrap-receipt.json
SSH_HOST_KEY=/etc/ssh/ssh_host_ed25519_key.pub
SSH_FINGERPRINT_PROPERTY=/SandboxSBOMReporting/SshHostEd25519Fingerprint
[[ -r "$STATE" ]]

ssh_active="$(systemctl is-active ssh.service 2>/dev/null || true)"
ssh_enabled="$(systemctl is-enabled ssh.service 2>/dev/null || true)"
guest_active="$(systemctl is-active virtualbox-guest-utils.service 2>/dev/null || true)"
guest_enabled="$(systemctl is-enabled virtualbox-guest-utils.service 2>/dev/null || true)"
sshd_path="$(command -v sshd || true)"
guest_path="$(command -v VBoxService || true)"
vbox_control_path="$(command -v VBoxControl || true)"
sshd_version="$(dpkg-query -W -f='${Version}' openssh-server 2>/dev/null || true)"
guest_version="$(dpkg-query -W -f='${Version}' virtualbox-guest-utils 2>/dev/null || true)"
ssh_fingerprint="$(ssh-keygen -lf "$SSH_HOST_KEY" -E sha256 2>/dev/null | awk '{print $2}')"
[[ "$ssh_fingerprint" =~ ^SHA256:[A-Za-z0-9+/]{20,}={0,2}$ ]]
[[ -n "$vbox_control_path" ]]
VBoxControl guestproperty set "$SSH_FINGERPRINT_PROPERTY" "$ssh_fingerprint"

python3 - "$STATE" "$RECEIPT" "$sshd_path" "$sshd_version" "$guest_path" "$guest_version" "$ssh_fingerprint" \
  "$ssh_active" "$ssh_enabled" "$guest_active" "$guest_enabled" <<'PY'
import json
import sys
from datetime import datetime, timezone

(
    state_path, destination, sshd_path, sshd_version, guest_path, guest_version, ssh_fingerprint,
    ssh_active, ssh_enabled, guest_active, guest_enabled,
) = sys.argv[1:]
with open(state_path, encoding="utf-8") as handle:
    state = json.load(handle)
failures = []
checks = {
    "sshd is installed": bool(sshd_path),
    "VirtualBox guest service is installed": bool(guest_path),
    "ssh.service is active": ssh_active == "active",
    "ssh.service is enabled": ssh_enabled == "enabled",
    "virtualbox-guest-utils.service is active": guest_active == "active",
    "virtualbox-guest-utils.service is enabled": guest_enabled == "enabled",
}
failures.extend(message for message, passed in checks.items() if not passed)
receipt = {
    "schema": "sandbox-sbom-reporting/ubuntu-base-bootstrap@2",
    "os": state["os"],
    "vm": state["vm"],
    "tools": {
        "sshd": {"path": sshd_path or None, "version": sshd_version or None},
        "virtualBoxGuestService": {"path": guest_path or None, "version": guest_version or None},
    },
    "services": {
        "ssh": {"activeState": ssh_active, "enabledState": ssh_enabled},
        "virtualBoxGuestUtils": {"activeState": guest_active, "enabledState": guest_enabled},
    },
    "sshHostKey": {"algorithm": "ssh-ed25519", "fingerprint": ssh_fingerprint},
    "verifiedAt": datetime.now(timezone.utc).isoformat(),
    "failures": failures,
    "outcome": "PASS" if not failures else "FAIL",
}
with open(destination, "w", encoding="utf-8") as handle:
    json.dump(receipt, handle, indent=2, sort_keys=True)
    handle.write("\n")
raise SystemExit(0 if not failures else 1)
PY
FINALIZER
chmod 0755 "$FINALIZER"

cat > "$SERVICE" <<'UNIT'
[Unit]
Description=Write the Sandbox SBOM Ubuntu base-bootstrap receipt
After=network-online.target ssh.service virtualbox-guest-utils.service
Wants=network-online.target ssh.service virtualbox-guest-utils.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/sandbox-base-bootstrap-finalize
UNIT

cat > "$TIMER" <<'UNIT'
[Unit]
Description=Validate the Sandbox SBOM Ubuntu base after first boot

[Timer]
OnBootSec=30s
AccuracySec=1s
Unit=sandbox-base-bootstrap-receipt.service

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable sandbox-base-bootstrap-receipt.timer
echo "base bootstrap installed; first-boot receipt validation is enabled"