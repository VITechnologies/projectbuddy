#!/usr/bin/env bash
set -euo pipefail

# Dry-run-first VirtualBox builder for a Windows 11 Pro guest intended to run
# LabVIEW 2026 Q3 Professional + VIPM Professional as a self-hosted GitHub
# Actions Windows runner for the labview-benchmark-actor-plugins module
# release pipeline (issue #127). Mirrors the conventions established by
# ../ubuntu-labview/build-virtualbox.sh: manifest-driven hardware profile,
# NVMe-only durable storage, ISO hash verification, and dry-run by default.
#
# This script only creates the VM shell and launches the unattended Windows
# Setup phase (OS install + Guest Additions). It does not install LabVIEW,
# VIPM, or register a GitHub Actions runner; those are later phases.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFEST="${PROVISIONING_MANIFEST:-$SCRIPT_DIR/provisioning-manifest.json}"
# VM_NAME, BASEFOLDER, GUEST_FULLNAME, and GUEST_HOSTNAME have no default --
# this script is a generic engine shared by any fork; SandboxSBOMReporting's
# own real values are supplied explicitly by its own workflow inputs (see
# .github/workflows/provision-windows-labview-vm.yml), never baked in here.
VM_NAME="${VM_NAME:-}"
BASEFOLDER="${BASEFOLDER:-}"
ISO="${ISO:-}"
GUEST_USER="${GUEST_USER:-actor}"
GUEST_FULLNAME="${GUEST_FULLNAME:-}"
GUEST_HOSTNAME="${GUEST_HOSTNAME:-}"
START_MODE="${START_MODE:-headless}"
WINRM_HOST_PORT="${WINRM_HOST_PORT:-15985}"
# Carries the NSIS WinRM-setup injection point onto the guest from first
# boot, before WinRM or any other guest-automation channel exists (issue
# #200/epic #198 -- this is now the ONLY WinRM-setup mechanism; the old
# --post-install-command/FirstLogonCommands path is retired, see the
# comment where this script builds it, below). Attached on the SATA
# controller, port 1 (a dedicated, otherwise-unused port -- the disk is on
# SATA port 0) -- but only AFTER `VBoxManage unattended install` has
# already started the VM, never before. Confirmed live (2026-11-03):
# `VBoxManage unattended install --install-additions` unconditionally
# ejects ANY pre-existing optical media on ANY controller/port (tried both
# IDE port 0/device 1 and SATA port 1 -- not slot-specific) before
# constructing its own combined OS+answer-file+Guest-Additions .viso.
# `--dry-run` never surfaces this (it doesn't exercise ConstructMedia());
# only a real run does. This still lands well before OOBE, so there's no
# meaningful timing risk for anything that needs to read this drive later.
# Also clear of the later install-vipm/install-labview phases' own media,
# which use IDE port 1 post-creation once WinRM already works (see
# provision-windows-labview-vm.yml).
#
# Left empty here (not a fixed default): auto-built fresh from
# winrm-setup-installer.nsi further down unless the caller explicitly
# overrides it with a path to a pre-built ISO (e.g. a pinned/pre-verified
# build for a specific CI run).
NSIS_BOOTSTRAP_ISO="${NSIS_BOOTSTRAP_ISO:-}"
# Research prototype (issue #191's recommended next step): an independent,
# second auxiliary ISO carrying winrm-service-bootstrap-installer.nsi's
# compiled output (a genuine Windows-Service-based WinRM bootstrap,
# alongside -- not replacing -- the proven VNC-triggered
# winrm-setup-installer.exe flow above). Strictly opt-in (empty/unset
# changes nothing about this script's existing, proven behavior): set
# ENABLE_SERVICE_BOOTSTRAP=1 to auto-build it fresh from
# winrm-service-bootstrap-installer.nsi, or set SERVICE_BOOTSTRAP_ISO
# directly to a pre-built ISO path (which also enables it). Attached on a
# NEW SATA port 2 (bumping --portcount from 2 to 3 only when enabled),
# following NSIS_BOOTSTRAP_ISO's exact empty-drive-then-swap-in-after-
# unattended-install-starts pattern -- see that variable's own comment for
# why (VBoxManage unattended install unconditionally ejects any
# pre-existing optical media on any controller/port before constructing
# its own install media).
ENABLE_SERVICE_BOOTSTRAP="${ENABLE_SERVICE_BOOTSTRAP:-0}"
SERVICE_BOOTSTRAP_ISO="${SERVICE_BOOTSTRAP_ISO:-}"
[[ -z "$SERVICE_BOOTSTRAP_ISO" ]] || ENABLE_SERVICE_BOOTSTRAP=1
DRY_RUN=1

usage() {
  cat <<'EOF'
Usage: ./build-virtualbox.sh [--dry-run|--run] [--gui|--headless|--no-start]

Required environment variables (no defaults -- this is a generic engine
shared by any fork): VM_NAME, BASEFOLDER, GUEST_FULLNAME, GUEST_HOSTNAME.

Dry-run is the default. A real build requires ISO and GUEST_PASSWORD_FILE
(a non-empty, mode-0600-or-stricter file holding a disposable local account
password; never pass it on the command line).
BASEFOLDER must resolve to an internal NVMe-backed filesystem.

The NSIS WinRM-setup injection point (see build-nsis-bootstrap-iso.sh) is
auto-built and attached on SATA port 1, moments after the VM starts (not
before -- see this script's NSIS_BOOTSTRAP_ISO comment for why). Set
NSIS_BOOTSTRAP_ISO to a pre-built ISO to override the auto-build.

Research prototype, strictly opt-in (set ENABLE_SERVICE_BOOTSTRAP=1 or
SERVICE_BOOTSTRAP_ISO to a pre-built ISO path; default is off, identical
to this script's prior behavior): also attaches the Windows-Service-based
WinRM bootstrap installer (see build-winrm-service-bootstrap-iso.sh) on a
new SATA port 2, alongside -- not replacing -- the NSIS_BOOTSTRAP_ISO/VNC
flow above.
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

[[ -r "$MANIFEST" ]] || { echo "manifest not readable: $MANIFEST" >&2; exit 2; }
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 2; }
if ((DRY_RUN == 0)); then
  command -v VBoxManage >/dev/null || { echo "VBoxManage is required" >&2; exit 2; }
fi

IFS=$'\t' read -r os_type image_index iso_filename iso_sha256 disk_gb memory_mb cpus vram_mb firmware chipset tpm_type < <(
  python3 - "$MANIFEST" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    manifest = json.load(handle)
os_contract = manifest["os"]
profile = manifest["virtualBox"]
iso = os_contract["iso"]
values = (
    os_contract["virtualBoxOsType"], os_contract["virtualBoxImageIndex"],
    iso["filename"], iso["sha256"],
    profile["diskGb"], profile["memoryMb"], profile["cpus"], profile["vramMb"],
    profile["firmware"], profile["chipset"], profile["tpmType"],
)
if any("\t" in str(value) or "\n" in str(value) for value in values):
    raise SystemExit("manifest values must be single-line")
print("\t".join(str(value) for value in values))
PY
)

[[ "$os_type" == Windows11_64 ]] || { echo "unsupported VirtualBox OS type: $os_type" >&2; exit 2; }
[[ "$iso_filename" == Win11_25H2_English_x64_v2.iso ]] || { echo "unexpected Windows ISO filename" >&2; exit 2; }
[[ "$iso_sha256" == 768984706b909479417b2368438909440f2967ff05c6a9195ed2667254e465e3 ]] || { echo "unexpected Windows ISO SHA-256" >&2; exit 2; }
[[ "$disk_gb" == 120 && "$memory_mb" == 8192 && "$cpus" == 4 && "$vram_mb" == 128 ]] || {
  echo "unexpected VirtualBox hardware profile" >&2
  exit 2
}
[[ "$firmware" == efi64 && "$chipset" == ich9 && "$tpm_type" == 2.0 ]] || {
  echo "unexpected VirtualBox firmware/chipset/TPM profile" >&2
  exit 2
}
[[ "$VM_NAME" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "VM_NAME contains unsupported characters" >&2; exit 2; }
[[ "$GUEST_USER" =~ ^[A-Za-z][A-Za-z0-9_-]*$ ]] || { echo "GUEST_USER is invalid" >&2; exit 2; }

if ((DRY_RUN == 0)) && VBoxManage list vms | grep -Fq "\"$VM_NAME\""; then
  echo "VM already exists: $VM_NAME" >&2
  exit 3
fi

evidence_dir="$BASEFOLDER/evidence/$VM_NAME"
disk_path="$BASEFOLDER/$VM_NAME/$VM_NAME.vdi"
script_template="$SCRIPT_DIR/unattended-template.xml"
[[ -r "$script_template" ]] || { echo "Unattended answer-file template missing: $script_template" >&2; exit 2; }

temporary_dir="$(mktemp -d)"
trap 'rm -rf "$temporary_dir"' EXIT
password_copy="$temporary_dir/guest-password"
install -m 0600 /dev/null "$password_copy"

if ((DRY_RUN == 0)); then
  [[ -f "$ISO" ]] || { echo "--run requires ISO (expected $iso_filename)" >&2; exit 3; }
  [[ -f "${GUEST_PASSWORD_FILE:-}" && -s "$GUEST_PASSWORD_FILE" ]] || {
    echo "--run requires a non-empty GUEST_PASSWORD_FILE" >&2
    exit 3
  }
  [[ -z "$NSIS_BOOTSTRAP_ISO" || -f "$NSIS_BOOTSTRAP_ISO" ]] || {
    echo "NSIS_BOOTSTRAP_ISO is set but not a file: $NSIS_BOOTSTRAP_ISO" >&2
    exit 3
  }
  [[ -z "$SERVICE_BOOTSTRAP_ISO" || -f "$SERVICE_BOOTSTRAP_ISO" ]] || {
    echo "SERVICE_BOOTSTRAP_ISO is set but not a file: $SERVICE_BOOTSTRAP_ISO" >&2
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
  mkdir -p "$BASEFOLDER" "$evidence_dir" "$(dirname "$disk_path")"
  cp "$GUEST_PASSWORD_FILE" "$password_copy"
  VBoxManage unattended install --help 2>&1 | grep -q -- '--install-additions' || {
    echo "VirtualBox does not support unattended --install-additions" >&2
    exit 3
  }
fi

# WinRM setup is no longer injected via --post-install-command/
# FirstLogonCommands (issue #188/epic #198): a winlogon.exe-initiated
# restart unconditionally interrupts that race ~17-20 min after boot,
# regardless of what command is registered there or how fast/minimal it is
# -- confirmed by issue #199's control test (the restart still fires with
# zero custom FirstLogonCommands at all) after five independently-validated
# in-guest mitigation attempts all failed identically (epic #190). Instead,
# the NSIS WinRM-setup installer (winrm-setup-installer.nsi) is delivered
# via an auxiliary ISO and triggered over VNC once the guest is stable
# (vnc-bootstrap.py, issue #201) -- entirely decoupled from Windows Setup's
# own fragile multi-reboot sequence, not racing it. See
# vm/windows-labview/README.md's "epic #198" section for the full
# investigation trail. Enable-WinRmAutomation.ps1 is kept in the repo as a
# historical reference (the logic winrm-setup-installer.nsi replicates),
# but is no longer invoked anywhere in this script.
#
# Auto-build the auxiliary ISO here unless the caller already supplied one
# (e.g. a pinned/pre-verified build for a specific CI run) -- building it
# fresh keeps the attached media always in sync with tracked
# winrm-setup-installer.nsi source, the same principle this script
# previously applied to Enable-WinRmAutomation.ps1 (inline base64-encoded
# at build time, never a stale pre-encoded copy).
#
# CRITICAL: for a real run, this MUST be written to a path that persists
# for the VM's own lifetime ($BASEFOLDER/$VM_NAME/, deleted only by
# `VBoxManage unregistervm --delete` when the VM itself is torn down) --
# NOT this script's own $temporary_dir (cleaned up when THIS SCRIPT exits,
# which happens within seconds of starting the VM). Confirmed live
# (2026-11-03, issue #205): a medium attached from $temporary_dir still
# works for the guest's FIRST boot (the VM process keeps its own open file
# handle alive even after the host unlinks the path), but fails with
# `VERR_FILE_NOT_FOUND` on the NEXT power-on (e.g. install-vipm's own
# restart cycle, run as a separate later workflow dispatch) once that file
# handle is gone -- VirtualBox then refuses to even start the VM, since it
# re-validates every attached medium's backing file from scratch on each
# power-on. This bit two real production VM builds differently: it
# happened to not manifest for the first two attempts (apparently
# timing-dependent) but did on the third -- treat it as a real,
# structural bug, not a fluke, regardless of how often it reproduces.
if [[ -z "$NSIS_BOOTSTRAP_ISO" ]]; then
  nsis_builder="$SCRIPT_DIR/build-nsis-bootstrap-iso.sh"
  [[ -x "$nsis_builder" ]] || { echo "NSIS bootstrap ISO builder missing or not executable: $nsis_builder" >&2; exit 2; }
  if ((DRY_RUN == 0)); then
    NSIS_BOOTSTRAP_ISO="$("$nsis_builder" --out "$BASEFOLDER/$VM_NAME/nsis-bootstrap.iso")"
  else
    # Dry-run: no VM/persistent directory will actually be created, so
    # build into the temp dir purely to validate makensis/xorriso work;
    # nothing references this path afterward.
    NSIS_BOOTSTRAP_ISO="$("$nsis_builder" --out "$temporary_dir/nsis-bootstrap.iso")"
  fi
fi

if [[ "$ENABLE_SERVICE_BOOTSTRAP" == 1 && -z "$SERVICE_BOOTSTRAP_ISO" ]]; then
  service_builder="$SCRIPT_DIR/build-winrm-service-bootstrap-iso.sh"
  [[ -x "$service_builder" ]] || { echo "Service bootstrap ISO builder missing or not executable: $service_builder" >&2; exit 2; }
  # Same persistence requirement as NSIS_BOOTSTRAP_ISO above -- see that
  # block's CRITICAL comment for why a real run must not use
  # $temporary_dir.
  if ((DRY_RUN == 0)); then
    SERVICE_BOOTSTRAP_ISO="$("$service_builder" --out "$BASEFOLDER/$VM_NAME/service-bootstrap.iso")"
  else
    SERVICE_BOOTSTRAP_ISO="$("$service_builder" --out "$temporary_dir/service-bootstrap.iso")"
  fi
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
    printf '[dry-run] VBoxManage unattended install %s --iso=%s --user=%s --user-password-file=<redacted> --full-user-name=%q --hostname=%s --image-index=%s --install-additions --script-template=%s --start-vm=%s\n' \
      "$VM_NAME" "${ISO:-<iso>}" "$GUEST_USER" "$GUEST_FULLNAME" "$GUEST_HOSTNAME" "$image_index" "$script_template" "$START_MODE"
    return
  fi
  printf '+ VBoxManage unattended install %s --iso=%s --user=%s --user-password-file=<redacted> --full-user-name=%q --hostname=%s --image-index=%s --install-additions --script-template=%s --start-vm=%s\n' \
    "$VM_NAME" "$ISO" "$GUEST_USER" "$GUEST_FULLNAME" "$GUEST_HOSTNAME" "$image_index" "$script_template" "$START_MODE"
  VBoxManage unattended install "$VM_NAME" --iso="$ISO" --user="$GUEST_USER" \
    --user-password-file="$password_copy" --full-user-name="$GUEST_FULLNAME" \
    --hostname="$GUEST_HOSTNAME" --image-index="$image_index" --install-additions \
    --script-template="$script_template" --start-vm="$START_MODE"
}

run VBoxManage createvm --name "$VM_NAME" --ostype "$os_type" --register --basefolder "$BASEFOLDER"
run VBoxManage modifyvm "$VM_NAME" --memory "$memory_mb" --cpus "$cpus" --vram "$vram_mb" \
  --firmware "$firmware" --chipset "$chipset" --tpm-type "$tpm_type" --nested-paging on --ioapic on \
  --graphicscontroller vmsvga --audio-driver none
run VBoxManage modifynvram "$VM_NAME" inituefivarstore
run VBoxManage modifynvram "$VM_NAME" enrollmssignatures
run VBoxManage modifynvram "$VM_NAME" enrollorclpk
run VBoxManage modifynvram "$VM_NAME" secureboot --enable
run VBoxManage createmedium disk --filename "$disk_path" --size "$((disk_gb * 1024))" --format VDI --variant Standard
sata_portcount=2
if [[ "$ENABLE_SERVICE_BOOTSTRAP" == 1 ]]; then
  sata_portcount=3
fi
run VBoxManage storagectl "$VM_NAME" --name SATA --add sata --controller IntelAhci --portcount "$sata_portcount" --bootable on
run VBoxManage storageattach "$VM_NAME" --storagectl SATA --port 0 --device 0 --type hdd --medium "$disk_path"
if [[ -n "$NSIS_BOOTSTRAP_ISO" ]]; then
  # Attach an EMPTY drive at this slot now, not the real ISO -- `storageattach
  # --medium <iso>` on a port with no drive object yet fails outright
  # ("No drive attached to device slot..."), confirmed live. The real ISO is
  # swapped in below, once `unattended install`'s unconditional
  # eject-all-optical-media pass (see that step's comment) is safely past;
  # an already-empty drive has nothing for that pass to eject, so it's
  # unaffected.
  run VBoxManage storageattach "$VM_NAME" --storagectl SATA --port 1 --device 0 --type dvddrive --medium emptydrive
fi
if [[ "$ENABLE_SERVICE_BOOTSTRAP" == 1 ]]; then
  # Same empty-drive-then-swap-in pattern as NSIS_BOOTSTRAP_ISO above, on
  # the new SATA port 2 this block's portcount bump made room for.
  run VBoxManage storageattach "$VM_NAME" --storagectl SATA --port 2 --device 0 --type dvddrive --medium emptydrive
fi
run VBoxManage storagectl "$VM_NAME" --name IDE --add ide
run VBoxManage storageattach "$VM_NAME" --storagectl IDE --port 0 --device 0 --type dvddrive --medium "${ISO:-emptydrive}"
run VBoxManage modifyvm "$VM_NAME" --natpf1 "winrm,tcp,127.0.0.1,$WINRM_HOST_PORT,,5985"

if [[ "$START_MODE" != none ]]; then
  run_redacted
  # NSIS_BOOTSTRAP_ISO must be attached AFTER unattended install starts the
  # VM, never before: confirmed live (2026-11-03) that `VBoxManage
  # unattended install --install-additions` unconditionally ejects ANY
  # pre-existing optical media on ANY controller/port before constructing
  # its own install media (observed on both IDE port 0/device 1 and SATA
  # port 1 -- it is not slot-specific). SATA port 1 stays an empty,
  # controller-only slot through unattended install's own setup, then gets
  # populated here once the VM is already running and that ejection pass is
  # long past. This still lands well before OOBE/FirstLogonCommands (which
  # take several more minutes), so there is no meaningful timing risk for
  # anything that needs to read this drive later.
  if [[ -n "$NSIS_BOOTSTRAP_ISO" ]]; then
    if ((DRY_RUN)); then
      run VBoxManage storageattach "$VM_NAME" --storagectl SATA --port 1 --device 0 --type dvddrive --medium "$NSIS_BOOTSTRAP_ISO"
    else
      for _ in $(seq 1 12); do
        state="$(VBoxManage showvminfo "$VM_NAME" --machinereadable 2>/dev/null | sed -n 's/^VMState="\(.*\)"$/\1/p')"
        [[ "$state" == running ]] && break
        sleep 1
      done
      [[ "$state" == running ]] || { echo "VM did not reach 'running' in time for NSIS_BOOTSTRAP_ISO attach (state: ${state:-unknown})" >&2; exit 4; }
      run VBoxManage storageattach "$VM_NAME" --storagectl SATA --port 1 --device 0 --type dvddrive --medium "$NSIS_BOOTSTRAP_ISO"
    fi
  fi
  if [[ "$ENABLE_SERVICE_BOOTSTRAP" == 1 ]]; then
    if ((DRY_RUN)); then
      run VBoxManage storageattach "$VM_NAME" --storagectl SATA --port 2 --device 0 --type dvddrive --medium "$SERVICE_BOOTSTRAP_ISO"
    else
      for _ in $(seq 1 12); do
        state="$(VBoxManage showvminfo "$VM_NAME" --machinereadable 2>/dev/null | sed -n 's/^VMState="\(.*\)"$/\1/p')"
        [[ "$state" == running ]] && break
        sleep 1
      done
      [[ "$state" == running ]] || { echo "VM did not reach 'running' in time for SERVICE_BOOTSTRAP_ISO attach (state: ${state:-unknown})" >&2; exit 4; }
      run VBoxManage storageattach "$VM_NAME" --storagectl SATA --port 2 --device 0 --type dvddrive --medium "$SERVICE_BOOTSTRAP_ISO"
    fi
  fi
else
  if ((DRY_RUN)); then
    run "(unattended install skipped: --no-start)"
  fi
  if [[ -n "$NSIS_BOOTSTRAP_ISO" ]]; then
    echo "NOTE: --no-start set; NSIS_BOOTSTRAP_ISO was not attached (it must be attached after a later 'unattended install' call starts the VM, not before -- see this script's NSIS_BOOTSTRAP_ISO comment)." >&2
  fi
  if [[ "$ENABLE_SERVICE_BOOTSTRAP" == 1 ]]; then
    echo "NOTE: --no-start set; SERVICE_BOOTSTRAP_ISO was not attached (same reason as NSIS_BOOTSTRAP_ISO above)." >&2
  fi
fi

echo "VM shell plan complete for $VM_NAME (dry_run=$DRY_RUN)." >&2
