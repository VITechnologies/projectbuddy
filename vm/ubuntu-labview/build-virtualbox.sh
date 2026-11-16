#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LANE_PROFILE="${LANE_PROFILE:-}"
MANIFEST="${PROVISIONING_MANIFEST:-$SCRIPT_DIR/provisioning-manifest.json}"
# VM_NAME, BASEFOLDER, GUEST_FULLNAME, and GUEST_HOSTNAME have no default --
# this script is a generic engine shared by any fork; SandboxSBOMReporting's
# own real values are supplied explicitly by its own workflow inputs (see
# ../../.github/workflows/provision-ubuntu-vm.yml), never baked in here.
VM_NAME="${VM_NAME:-}"
BASEFOLDER="${BASEFOLDER:-}"
ISO="${ISO:-}"
GUEST_USER="${GUEST_USER:-actor}"
GUEST_FULLNAME="${GUEST_FULLNAME:-}"
GUEST_HOSTNAME="${GUEST_HOSTNAME:-}"
SSH_HOST_PORT="${SSH_HOST_PORT:-2222}"
START_MODE="${START_MODE:-gui}"
DRY_RUN=1

usage() {
  cat <<'EOF'
Usage: ./build-virtualbox.sh [--dry-run|--run] [--gui|--headless|--no-start]

Required environment variables (no defaults -- this is a generic engine
shared by any fork): VM_NAME, BASEFOLDER, GUEST_FULLNAME, GUEST_HOSTNAME.

The OS, ISO, and hardware come from PROVISIONING_MANIFEST (default: the
LabVIEW appliance's provisioning-manifest.json, with pinned hardware) or,
for a LabVIEW-free lane base VM, from LANE_PROFILE
(projectbuddy/ubuntu-lane-profile@1, hardware taken from the profile).
Set at most one of them.

Dry-run is the default. A real build requires ISO and GUEST_PASSWORD_FILE.
BASEFOLDER must resolve to an internal NVMe-backed filesystem.
EOF
}

while (($#)); do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --run) DRY_RUN=0 ;;
    --gui) START_MODE=gui ;;
    --headless) START_MODE=headless ;;
    --no-start) START_MODE=none ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

for required_var in VM_NAME BASEFOLDER GUEST_FULLNAME GUEST_HOSTNAME; do
  [[ -n "${!required_var}" ]] || { echo "$required_var is required (no default -- see --help)" >&2; usage >&2; exit 2; }
done

if [[ -n "$LANE_PROFILE" ]]; then
  [[ -z "${PROVISIONING_MANIFEST:-}" ]] || { echo "set LANE_PROFILE or PROVISIONING_MANIFEST, not both" >&2; exit 2; }
  SOURCE_KIND=lane
  SOURCE="$LANE_PROFILE"
else
  SOURCE_KIND=appliance
  SOURCE="$MANIFEST"
fi
[[ -r "$SOURCE" ]] || { echo "$SOURCE_KIND source not readable: $SOURCE" >&2; exit 2; }
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 2; }
if ((DRY_RUN == 0)); then
  command -v VBoxManage >/dev/null || { echo "VBoxManage is required" >&2; exit 2; }
fi

# A lane profile is validated by the same code as Invoke-LaneBox.py validate-profile.
source_values="$(python3 - "$SOURCE_KIND" "$SOURCE" "$SCRIPT_DIR/../../scripts" <<'PY'
import json
import pathlib
import sys

kind, path, scripts = sys.argv[1:]
if kind == "lane":
    sys.path.insert(0, scripts)
    from lane_box.common import LaneBoxError
    from lane_box.profile import load_profile

    try:
        profile = load_profile(pathlib.Path(path))
    except LaneBoxError as error:
        raise SystemExit(f"lane profile refused: {error}")
    os_contract, hardware, label = profile.os, profile.virtual_box, f"{profile.name} ({profile.sha256})"
else:
    try:
        with open(path, encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SystemExit(f"manifest is unreadable: {error}")
    if not isinstance(manifest, dict) or manifest.get("schema") != "projectbuddy/ubuntu-labview-provisioning@1":
        raise SystemExit("manifest schema must be projectbuddy/ubuntu-labview-provisioning@1")
    os_contract, hardware, label = manifest.get("os"), manifest.get("virtualBox"), "-"
try:
    iso = os_contract["iso"]
    values = (
        os_contract["virtualBoxOsType"], iso["filename"], iso["url"], iso["sha256"],
        hardware["diskGb"], hardware["memoryMb"], hardware["cpus"], hardware["vramMb"],
    )
except (KeyError, TypeError) as error:
    raise SystemExit(f"manifest is missing an os, iso, or virtualBox value: {error}")
if not all(type(value) is int for value in values[4:]):
    raise SystemExit("virtualBox values must be integers")
values += (hardware["diskGb"] * 1024, label)
if any("\t" in str(value) or "\n" in str(value) for value in values):
    raise SystemExit("manifest values must be single-line")
print("\t".join(str(value) for value in values))
PY
)" || exit 2
IFS=$'\t' read -r os_type iso_filename iso_url iso_sha256 disk_gb memory_mb cpus vram_mb disk_mb source_label <<<"$source_values"

[[ "$os_type" == Ubuntu24_LTS_64 ]] || { echo "unsupported VirtualBox OS type: $os_type" >&2; exit 2; }
[[ "$iso_filename" == ubuntu-24.04.4-desktop-amd64.iso ]] || { echo "unexpected Ubuntu ISO filename" >&2; exit 2; }
[[ "$iso_url" == https://releases.ubuntu.com/24.04/ubuntu-24.04.4-desktop-amd64.iso ]] || { echo "unexpected Ubuntu ISO URL" >&2; exit 2; }
[[ "$iso_sha256" == 3a4c9877b483ab46d7c3fbe165a0db275e1ae3cfe56a5657e5a47c2f99a99d1e ]] || { echo "unexpected Ubuntu ISO SHA-256" >&2; exit 2; }
if [[ "$SOURCE_KIND" == appliance ]]; then
  [[ "$disk_gb" == 80 && "$memory_mb" == 12288 && "$cpus" == 6 && "$vram_mb" == 128 ]] || {
    echo "unexpected VirtualBox hardware profile" >&2
    exit 2
  }
fi
if [[ "$SSH_HOST_PORT" =~ ^[0-9]+$ ]]; then
  if ((SSH_HOST_PORT < 1024 || SSH_HOST_PORT > 65535)); then
    echo "SSH_HOST_PORT must be an integer from 1024 through 65535" >&2
    exit 2
  fi
else
  echo "SSH_HOST_PORT must be an integer from 1024 through 65535" >&2
  exit 2
fi
[[ "$VM_NAME" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "VM_NAME contains unsupported characters" >&2; exit 2; }
[[ "$GUEST_USER" =~ ^[a-z_][a-z0-9_-]*$ ]] || { echo "GUEST_USER is invalid" >&2; exit 2; }

if ((DRY_RUN == 0)) && VBoxManage list vms | grep -Fq "\"$VM_NAME\""; then
  echo "VM already exists: $VM_NAME" >&2
  exit 3
fi

BOOTSTRAP_SOURCE="$SCRIPT_DIR/base-bootstrap.sh"
[[ -r "$BOOTSTRAP_SOURCE" ]] || { echo "base bootstrap missing: $BOOTSTRAP_SOURCE" >&2; exit 2; }
evidence_dir="$BASEFOLDER/evidence/$VM_NAME"

temporary_dir="$(mktemp -d)"
trap 'rm -rf "$temporary_dir"' EXIT
password_copy="$temporary_dir/guest-password"
bootstrap_template="$temporary_dir/base-bootstrap.sh"
install -m 0600 /dev/null "$password_copy"

if ((DRY_RUN == 0)); then
  [[ -f "$ISO" ]] || { echo "--run requires ISO (expected $iso_filename from $iso_url)" >&2; exit 3; }
  [[ -f "${GUEST_PASSWORD_FILE:-}" && -s "$GUEST_PASSWORD_FILE" ]] || {
    echo "--run requires a non-empty GUEST_PASSWORD_FILE" >&2
    exit 3
  }
  password_mode="$(stat -c '%a' "$GUEST_PASSWORD_FILE")"
  (((8#$password_mode & 077) == 0)) || { echo "GUEST_PASSWORD_FILE must have mode 0600 or stricter" >&2; exit 3; }
  printf '%s  %s\n' "$iso_sha256" "$ISO" | sha256sum --check
  storage_probe="$BASEFOLDER"
  while [[ ! -e "$storage_probe" && "$storage_probe" != / ]]; do
    storage_probe="$(dirname "$storage_probe")"
  done
  read -r nvme_source mount_options < <(findmnt -n -o SOURCE,OPTIONS --target "$storage_probe" | tail -n 1)
  [[ "$nvme_source" == /dev/nvme* ]] || { echo "BASEFOLDER is not on internal NVMe: $nvme_source" >&2; exit 3; }
  [[ ",$mount_options," == *,rw,* ]] || { echo "BASEFOLDER NVMe is not writable: $nvme_source ($mount_options)" >&2; exit 3; }
  mkdir -p "$BASEFOLDER" "$evidence_dir"
  cp "$GUEST_PASSWORD_FILE" "$password_copy"
  VBoxManage unattended install --help 2>&1 | grep -q -- '--post-install-template' || {
    echo "VirtualBox does not support unattended --post-install-template" >&2
    exit 3
  }
fi

run() {
  if ((DRY_RUN)); then
    printf '[dry-run]'
    printf ' %s' "$@"
    printf '\n'
  else
    printf '+'
    printf ' %q' "$@"
    printf '\n'
    "$@"
  fi
}

run_redacted() {
  if ((DRY_RUN)); then
    run "$@"
    return
  fi
  printf '+'
  printf ' %q' "$@"
  printf '\n'
  set +e
  "$@" 2>&1 | sed -E 's/^([[:space:]]*(password|user-password|admin-password)[[:space:]]*=).*/\1 [redacted]/I'
  command_status="${PIPESTATUS[0]}"
  set -e
  return "$command_status"
}

vm_dir="$BASEFOLDER/$VM_NAME"
disk="$vm_dir/$VM_NAME.vdi"
echo "VirtualBox build: $VM_NAME"
[[ "$SOURCE_KIND" == appliance ]] || echo "Lane profile: $source_label"
echo "Profile: $os_type | ${memory_mb} MB | ${cpus} CPU | ${vram_mb} MB VRAM | ${disk_gb} GB"
echo "Storage: $BASEFOLDER (real builds require an NVMe source reported by findmnt)"
echo "ISO contract: $iso_filename | $iso_sha256"

run VBoxManage createvm --name "$VM_NAME" --ostype "$os_type" --basefolder "$BASEFOLDER" --register
run VBoxManage modifyvm "$VM_NAME" --memory "$memory_mb" --cpus "$cpus" --vram "$vram_mb" \
  --graphicscontroller vmsvga --chipset piix3 --firmware bios --ioapic on --rtcuseutc on --nic1 nat
run VBoxManage modifyvm "$VM_NAME" --natpf1 "ssh,tcp,127.0.0.1,$SSH_HOST_PORT,,22"
run VBoxManage createmedium disk --filename "$disk" --size "$disk_mb" --format VDI
run VBoxManage storagectl "$VM_NAME" --name SATA --add sata --controller IntelAhci --portcount 2 --bootable on
run VBoxManage storageattach "$VM_NAME" --storagectl SATA --port 0 --device 0 --type hdd --medium "$disk"
run VBoxManage storagectl "$VM_NAME" --name IDE --add ide --controller PIIX4

if ((DRY_RUN)); then
  vm_uuid=00000000-0000-4000-8000-000000000000
else
  vm_uuid="$(VBoxManage showvminfo "$VM_NAME" --machinereadable | sed -n 's/^UUID="\(.*\)"$/\1/p')"
  [[ -n "$vm_uuid" ]] || { echo "could not resolve VM UUID" >&2; exit 3; }
fi
sed -e "s/@@VM_NAME@@/$VM_NAME/g" -e "s/@@VM_UUID@@/$vm_uuid/g" "$BOOTSTRAP_SOURCE" > "$bootstrap_template"
chmod 0600 "$bootstrap_template"

unattended=(
  VBoxManage unattended install "$VM_NAME"
  "--iso=${ISO:-/path/to/$iso_filename}"
  "--user=$GUEST_USER" "--user-password-file=$password_copy" "--full-user-name=$GUEST_FULLNAME"
  --locale=en_US --country=US --time-zone=UTC "--hostname=$GUEST_HOSTNAME.local"
  "--post-install-template=$bootstrap_template" --no-install-additions
)
[[ "$START_MODE" == none ]] || unattended+=("--start-vm=$START_MODE")
run_redacted "${unattended[@]}"

cat <<EOF
Next after unattended installation and first boot:
  enroll the guest SSH key with enroll-vm-ssh-host-key.py using VM UUID $vm_uuid
  use StrictHostKeyChecking=yes and the enrolled known-hosts file for every SSH/SCP operation
  retrieve $evidence_dir/base-bootstrap-receipt.json and verify its VM UUID and SSH fingerprint
EOF
if [[ "$SOURCE_KIND" == lane ]]; then
  echo "  power off and snapshot; then continue at step 2 of docs/vm-provisioning/ubuntu-lane-box.md (install-toolchain)"
  exit 0
fi
cat <<EOF
  upload the manifest, provisioner, and NI key; then run provision-guest.sh as root
EOF