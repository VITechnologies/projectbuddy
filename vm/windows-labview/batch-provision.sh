#!/usr/bin/env bash
# Serially provision one or more Windows LabVIEW runner VMs from
# runner-inventory.json, stopping automation at "ready for the operator to
# activate LabVIEW/VIPM" -- activation is the ONE remaining manual,
# interactive, human-gated step (see README.md's operator runbook).
# create-vm, install-vipm, install-labview, AND runner registration
# (register-runner, over WinRM, no interactive session) are all automated: a
# VM that reaches "ready-for-activation" is already a registered, online
# GitHub Actions runner for its target repo -- just not yet licensed.
#
# Processes every entry with "status": "queued" in order, one VM at a time
# (concurrent create-vm/install-vipm/install-labview runs contend heavily for
# host CPU/disk during first-boot Windows Update/Defender housekeeping --
# confirmed live 2026-11-02, see README's "known operational constraint"
# note). A human can activate any VM that reaches "ready-for-activation"
# while this script keeps building the next one. The moment a VM reaches
# "ready-for-activation" this script calls vnc-console.sh to automatically
# pop open a VNC console for it (see issue #176) -- no manual VRDE/remmina
# setup needed before activating LabVIEW/VIPM.
#
# A transient per-VM failure (of any kind) is retried up to MAX_ATTEMPTS
# times (default 3; see issue #204), tearing the VM down and rebuilding it
# from scratch between attempts -- create-vm refuses to proceed if a VM by
# that name already exists.
#
# Usage:
#   ./batch-provision.sh [--inventory PATH] [--dry-run]
#
# Add VMs to build by editing runner-inventory.json directly: add an entry
# with "status": "queued" and the vmName/winrmHostPort/targetRepo/runnerName/
# runnerLabels/dedicatedServiceAccount you want (see the "Adding a VM to the
# batch queue" section of README.md). register-runner requires a
# RUNNER_REGISTRATION_PAT repo secret -- see README for scope/setup.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
INVENTORY="$SCRIPT_DIR/runner-inventory.json"
DRY_RUN=0
# GH_REPO, STORAGE_ROOT, WIN_ISO, VIPM_INSTALLER, and LABVIEW_ISO have no
# default -- this script is a generic engine shared by any fork; set them
# as environment variables before running with your own fork's real,
# consumer-specific values (never committed here). STORAGE_ROOT
# additionally has operational history worth knowing before overriding:
# /run/media/sergio/Data (the original storage_root) filled to 0 bytes free
# on 2026-11-02 while building the core/launcher VMs -- see runner-inventory.json's
# hostCapacity.notes. An external cold-storage-only drive exists for
# relocating powered-off/failed VM disks, never as a live VM storage_root.
# Check real headroom with `df -h` before provisioning.
GH_REPO="${GH_REPO:-}"
STORAGE_ROOT="${STORAGE_ROOT:-}"
WIN_ISO="${WIN_ISO:-}"
VIPM_INSTALLER="${VIPM_INSTALLER:-}"
LABVIEW_ISO="${LABVIEW_ISO:-}"

# How long to wait for the FIRST boot's unattended Windows Setup + WinRM
# self-enable to finish, polling every POLL_INTERVAL seconds. install-vipm
# and install-labview each handle their own internal power-cycle/wait-ready
# polling inside the workflow, so this is the one wait this script owns.
FIRST_BOOT_TIMEOUT_SEC="${FIRST_BOOT_TIMEOUT_SEC:-3600}"
POLL_INTERVAL="${POLL_INTERVAL:-20}"

# Bounded auto-retry for transient per-VM failures (issue #204): a fresh
# VirtualBox/host-side hiccup, a dropped WinRM connection, etc. shouldn't
# require a full manual re-dispatch. 3 total attempts (2 retries) by
# default -- bounded, not infinite, and configurable. Each failed attempt's
# VM is fully torn down (powered off + unregistered + disk deleted) before
# the next attempt, since create-vm refuses to proceed if a VM by that name
# already exists.
MAX_ATTEMPTS="${MAX_ATTEMPTS:-3}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --inventory) INVENTORY="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

log() { printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$1" >&2; }

run() {
  if [[ "$DRY_RUN" == "1" ]]; then
    printf '[dry-run] %s\n' "$*"
  else
    "$@"
  fi
}

set_status() {
  local vm_name="$1" status="$2" note="${3:-}"
  [[ "$DRY_RUN" == "1" ]] && { log "[dry-run] would set $vm_name status=$status note=$note"; return 0; }
  local tmp; tmp="$(mktemp)"
  jq --arg vm "$vm_name" --arg status "$status" --arg note "$note" \
    '(.vms[] | select(.vmName == $vm)) |= (.status = $status | if $note != "" then .notes = $note else . end)' \
    "$INVENTORY" > "$tmp"
  mv "$tmp" "$INVENTORY"
}

# Dispatch a workflow_dispatch run and block until it completes, printing its
# conclusion. Returns the run's conclusion on stdout ("success"/"failure"/...).
dispatch_and_wait() {
  local phase="$1"; shift
  log "Dispatching phase=$phase with: $*"
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "success"
    return 0
  fi
  local before_run_id
  before_run_id="$(gh run list --repo "$GH_REPO" --workflow=provision-windows-labview-vm.yml --limit 1 --json databaseId -q '.[0].databaseId // 0')"
  # Redirect gh's own confirmation text (it prints the new run's URL to stdout)
  # to stderr -- this function's stdout is captured by the caller as the
  # eventual conclusion, and must contain nothing else.
  gh workflow run provision-windows-labview-vm.yml --repo "$GH_REPO" --ref main -f "phase=$phase" "$@" >&2
  local run_id=""
  for _ in $(seq 1 12); do
    sleep 5
    run_id="$(gh run list --repo "$GH_REPO" --workflow=provision-windows-labview-vm.yml --limit 1 --json databaseId -q '.[0].databaseId // 0')"
    [[ "$run_id" != "$before_run_id" && "$run_id" != "0" ]] && break
  done
  [[ -n "$run_id" && "$run_id" != "0" ]] || { log "ERROR: could not find the new workflow run"; echo "failure"; return 0; }
  log "Watching run $run_id ..."
  while true; do
    local status conclusion
    status="$(gh run view "$run_id" --repo "$GH_REPO" --json status -q '.status')"
    [[ "$status" == "completed" ]] && break
    sleep 15
  done
  conclusion="$(gh run view "$run_id" --repo "$GH_REPO" --json conclusion -q '.conclusion')"
  log "Run $run_id concluded: $conclusion"
  echo "$conclusion"
}

wait_for_first_boot_winrm() {
  local port="$1" vm_name="$2"
  log "Waiting up to ${FIRST_BOOT_TIMEOUT_SEC}s for $vm_name's first-boot WinRM to respond on port $port ..."
  [[ "$DRY_RUN" == "1" ]] && { log "[dry-run] would wait for WinRM"; return 0; }
  # Track REAL wall-clock elapsed time (not an incremented counter): a failed
  # wait-ready attempt can itself block for up to its own ~55-60s read
  # timeout when TCP is open but WinRM's HTTP layer never responds, so
  # incrementing by just $POLL_INTERVAL per loop undercounted real elapsed
  # time by roughly 4x in practice (confirmed live 2026-11-02 -- a VM stuck
  # in that exact state took ~4 real hours to hit a "3600s" timeout).
  local start_epoch; start_epoch="$(date +%s)"
  local next_heartbeat=300
  local venv="/tmp/batch-provision-winrm-venv"
  local probe_status
  if [[ ! -x "$venv/bin/python3" ]]; then
    python3 -m venv "$venv"
    "$venv/bin/pip" install --quiet pywinrm
  fi
  while true; do
    local elapsed=$(( $(date +%s) - start_epoch ))
    (( elapsed >= FIRST_BOOT_TIMEOUT_SEC )) && break
    # Fail fast if the VM itself has died rather than waiting out the full
    # timeout polling a corpse -- confirmed live 2026-11-03: a VirtualBox
    # internal bug (XPCOM "AddRef: illegal refcnt" assertion in
    # ipcDConnectService.cpp, VBoxSVC crashing shortly after) powered off
    # four VMs within 15-46s of "successfully started", but this loop still
    # waited the full 3600s each on pure WinRM silence before reporting
    # failure. A clean retry after VBoxSVC's next auto-respawn ran fine, so
    # this is a transient host-side fault, not a guest/Windows Setup issue --
    # surface it immediately so an operator can retry sooner.
    local vm_state; vm_state="$(VBoxManage showvminfo "$vm_name" --machinereadable 2>/dev/null | sed -n 's/^VMState="\(.*\)"$/\1/p')"
    if [[ "$vm_state" != "running" ]]; then
      log "ERROR: $vm_name is no longer running (VMState=$vm_state) after ${elapsed}s -- likely a VirtualBox-side crash, not a WinRM delay. Check VBox.log and VBoxSVC health before retrying."
      return 1
    fi
    if timeout 3 bash -c "cat < /dev/null > /dev/tcp/127.0.0.1/$port" 2>/dev/null; then
      if "$venv/bin/python3" "$REPO_ROOT/vm/windows-labview/provision-windows-guest.py" \
          --port "$port" --password-file "$WINRM_PASSWORD_FILE" wait-ready >/dev/null 2>&1; then
        log "$vm_name WinRM is ready after ${elapsed}s."
        return 0
      else
        probe_status=$?
        if [[ "$probe_status" -eq 3 ]]; then
          log "ERROR: $vm_name WinRM rejected guest credentials; preserving it for credential correction."
          return 3
        fi
      fi
    fi
    elapsed=$(( $(date +%s) - start_epoch ))
    # Progress heartbeat every ~5 min of REAL time so a human or future agent
    # tailing the log can tell this is still polling, not stuck silently.
    if (( elapsed >= next_heartbeat )); then
      log "$vm_name: still waiting for WinRM (${elapsed}s elapsed, timeout ${FIRST_BOOT_TIMEOUT_SEC}s) ..."
      next_heartbeat=$(( next_heartbeat + 300 ))
    fi
    sleep "$POLL_INTERVAL"
  done
  log "ERROR: $vm_name WinRM did not become ready within ${FIRST_BOOT_TIMEOUT_SEC}s."
  return 1
}

# Tear down a failed/poisoned attempt before a retry or after the last failed
# attempt. Preserve shared installer media and refuse ambiguous VM identities.
cleanup_vm_for_retry() {
  local vm_name="$1"
  if [[ "$DRY_RUN" == "1" ]]; then
    log "[dry-run] would discard poisoned VM $vm_name before retrying or exiting"
    return 0
  fi
  local matches uuid info actual_name cfg_file expected_dir expected_cfg state details disk_paths disk_path actual_disk
  local storage_root
  [[ "$vm_name" =~ ^[A-Za-z0-9._-]+$ && "$vm_name" != "." && "$vm_name" != ".." ]] || { log "ERROR: refusing teardown for invalid VM name '$vm_name'."; return 1; }
  [[ "$STORAGE_ROOT" == /* && -d "$STORAGE_ROOT" ]] || { log "ERROR: STORAGE_ROOT must be an existing absolute directory."; return 1; }
  storage_root="$(realpath -e "$STORAGE_ROOT")"
  local storage_device; storage_device="$(findmnt -n -o SOURCE --target "$storage_root")"
  [[ "$storage_device" == /dev/nvme* ]] || { log "ERROR: refusing teardown outside internal NVMe (STORAGE_ROOT device: $storage_device)."; return 1; }
  matches="$(VBoxManage list vms 2>/dev/null | awk -v expected="\"$vm_name\"" '$1 == expected { gsub(/[{}]/, "", $2); print $2 }')"
  [[ -n "$matches" ]] || { log "$vm_name: no registered VM remains to discard."; return 0; }
  [[ "$(printf '%s\n' "$matches" | wc -l)" -eq 1 ]] || { log "ERROR: multiple VirtualBox registrations match $vm_name; refusing teardown."; return 1; }
  uuid="$matches"
  info="$(VBoxManage showvminfo "$uuid" --machinereadable 2>/dev/null)" || { log "ERROR: could not inspect VM $vm_name ($uuid); refusing teardown."; return 1; }
  actual_name="$(printf '%s\n' "$info" | sed -n 's/^name="\(.*\)"$/\1/p')"
  cfg_file="$(printf '%s\n' "$info" | sed -n 's/^CfgFile="\(.*\)"$/\1/p')"
  local requested_dir="$storage_root/$vm_name"
  [[ ! -L "$requested_dir" ]] || { log "ERROR: refusing teardown through a symlinked VM directory: $requested_dir"; return 1; }
  expected_dir="$(realpath -m "$requested_dir")"
  [[ "$(dirname "$expected_dir")" == "$storage_root" ]] || { log "ERROR: VM directory escapes STORAGE_ROOT: $expected_dir"; return 1; }
  expected_cfg="$expected_dir/$vm_name.vbox"
  actual_cfg="$(realpath -m "$cfg_file")"
  [[ "$actual_name" == "$vm_name" && "$actual_cfg" == "$expected_cfg" ]] || {
    log "ERROR: VM identity/config path does not match $STORAGE_ROOT/$vm_name; refusing teardown."
    return 1
  }
  state="$(printf '%s\n' "$info" | sed -n 's/^VMState="\(.*\)"$/\1/p')"
  case "$state" in
    livesnapshotting|onlinesnapshotting)
      log "ERROR: $vm_name is snapshotting ($state); refusing to interrupt it."
      return 1
      ;;
    running|paused)
      log "$vm_name ($uuid): powering off poisoned attempt (state=$state)."
      VBoxManage controlvm "$uuid" poweroff >/dev/null 2>&1 || { log "ERROR: could not power off $vm_name ($uuid)."; return 1; }
      state="$(VBoxManage showvminfo "$uuid" --machinereadable 2>/dev/null | sed -n 's/^VMState="\(.*\)"$/\1/p')"
      [[ "$state" == "poweroff" || "$state" == "aborted" ]] || { log "ERROR: $vm_name did not reach a deletable state (VMState=$state)."; return 1; }
      ;;
    saved)
      VBoxManage discardstate "$uuid" >/dev/null 2>&1 || { log "ERROR: could not discard saved state for $vm_name ($uuid)."; return 1; }
      ;;
    poweroff|aborted) ;;
    *)
      log "ERROR: refusing teardown in unexpected VM state '$state' for $vm_name ($uuid)."
      return 1
      ;;
  esac

  details="$(VBoxManage showvminfo "$uuid" --details 2>/dev/null)" || { log "ERROR: could not inspect VM storage for $vm_name ($uuid)."; return 1; }
  disk_paths="$(printf '%s\n' "$details" | sed -n 's/^[[:space:]]*Location: "\(.*\)"$/\1/p' | grep -Ei '\.(vdi|vmdk|vhd)$' || true)"
  while IFS= read -r disk_path; do
    [[ -n "$disk_path" ]] || continue
    actual_disk="$(realpath -m "$disk_path")"
    [[ "$actual_disk" == "$expected_dir"/* ]] || { log "ERROR: attached virtual disk is outside the VM directory ($disk_path); refusing delete."; return 1; }
  done <<< "$disk_paths"

  local slot controller port device
  for slot in "IDE 0 0" "IDE 1 0" "IDE 1 1" "SATA 1 0" "SATA 2 0"; do
    read -r controller port device <<< "$slot"
    VBoxManage storageattach "$uuid" --storagectl "$controller" --port "$port" --device "$device" --type dvddrive --medium none >/dev/null 2>&1 || true
  done
  details="$(VBoxManage showvminfo "$uuid" --details 2>/dev/null)" || { log "ERROR: could not recheck detached media for $vm_name ($uuid)."; return 1; }
  if printf '%s\n' "$details" | grep -Eiq 'Location: ".*\.(iso|viso)"'; then
    log "ERROR: an optical image remains attached to $vm_name ($uuid); refusing unregister."
    return 1
  fi
  log "$vm_name ($uuid): unregistering and deleting the poisoned VM storage."
  VBoxManage unregistervm "$uuid" --delete >/dev/null 2>&1 || { log "ERROR: VBoxManage unregistervm failed for $vm_name ($uuid)."; return 1; }
  if VBoxManage list vms 2>/dev/null | grep -Fq "{$uuid}"; then
    log "ERROR: VM $vm_name ($uuid) remains registered after teardown."
    return 1
  fi
  if [[ -d "$expected_dir" && ! -L "$expected_dir" ]]; then
    [[ "$(realpath -m "$expected_dir")" == "$expected_dir" ]] || { log "ERROR: refusing to remove a redirected VM directory: $expected_dir"; return 1; }
    rm -rf -- "$expected_dir"
  fi
  log "$vm_name ($uuid): poisoned VM discarded."
}

# Derives a safe, batch-unique fallback guest hostname when
# runner-inventory.json doesn't supply one explicitly (issue #69: every VM
# in a batch previously fell back to the same literal workflow-dispatch
# placeholder, a real same-hostname collision). Windows NetBIOS hostnames
# are capped at 15 characters and must avoid `\/:*?"<>|` -- "vm-" (3 chars)
# + 8 lowercase hex chars from a stable hash of $vm_name fits in exactly
# 11 characters, well under the limit, and is deterministic (the same
# vm_name always derives the same hostname, so re-dispatching the same VM
# doesn't change its identity).
derive_guest_hostname() {
  local vm_name="$1"
  local digest
  digest="$(printf '%s' "$vm_name" | sha256sum | cut -c1-8)"
  printf 'vm-%s' "$digest"
}

[[ -f "$INVENTORY" ]] || { echo "Inventory file not found: $INVENTORY (copy runner-inventory.example.json to runner-inventory.json and fill in your own real VM entries to get started)" >&2; exit 2; }
command -v jq >/dev/null || { echo "jq is required" >&2; exit 2; }
for required_var in GH_REPO STORAGE_ROOT WIN_ISO VIPM_INSTALLER LABVIEW_ISO; do
  [[ -n "${!required_var}" ]] || { echo "$required_var is required (no default -- set it as an environment variable)" >&2; exit 2; }
done
WINRM_PASSWORD_FILE="${WINRM_PASSWORD_FILE:-/run/user/$(id -u)/win-vm-password}"
[[ -f "$WINRM_PASSWORD_FILE" ]] || { echo "WINRM_PASSWORD_FILE not found: $WINRM_PASSWORD_FILE (set the env var to override)" >&2; exit 2; }

mapfile -t QUEUED < <(jq -r '.vms[] | select(.status == "queued") | .vmName' "$INVENTORY")
if [[ ${#QUEUED[@]} -eq 0 ]]; then
  log "No queued VMs found in $INVENTORY. Add an entry with \"status\": \"queued\" to build one."
  exit 0
fi
log "Queued VMs to build serially: ${QUEUED[*]}"

# Runs ONE attempt at fully provisioning $vm_name through register-runner.
# Returns 0 on success, 1 on any phase failure -- sets FAILURE_REASON (a
# plain global, not local, so the caller can read it after the return)
# describing which phase failed, for status-note/log purposes. Does NOT
# call set_status itself on failure -- that's the retry wrapper's job,
# once all attempts are exhausted, so a transient failure that a retry
# fixes never gets recorded as "failed" in the inventory at all.
provision_vm_once() {
  local vm_name="$1" port="$2" repo="$3" runner_name="$4" runner_labels="$5" service_account="$6" guest_hostname="$7" guest_full_name="$8"
  local conclusion

  conclusion="$(dispatch_and_wait create-vm \
    -f "vm_name=$vm_name" -f "storage_root=$STORAGE_ROOT" -f "iso_path=$WIN_ISO" -f "winrm_host_port=$port" \
    -f "guest_hostname=$guest_hostname" -f "guest_full_name=$guest_full_name")"
  if [[ "$conclusion" != "success" ]]; then
    FAILURE_REASON="create-vm: $conclusion"; return 1
  fi

  if wait_for_first_boot_winrm "$port" "$vm_name"; then
    :
  else
    local winrm_status=$?
    if [[ "$winrm_status" -eq 3 ]]; then
      FAILURE_RETRYABLE=0
      FAILURE_REASON="first-boot WinRM rejected guest credentials"
    else
      FAILURE_REASON="first-boot WinRM never became ready"
    fi
    return 1
  fi

  conclusion="$(dispatch_and_wait install-vipm \
    -f "vm_name=$vm_name" -f "storage_root=$STORAGE_ROOT" -f "vipm_installer_path=$VIPM_INSTALLER" -f "winrm_host_port=$port")"
  if [[ "$conclusion" != "success" ]]; then
    FAILURE_REASON="install-vipm: $conclusion"; return 1
  fi

  conclusion="$(dispatch_and_wait install-labview \
    -f "vm_name=$vm_name" -f "storage_root=$STORAGE_ROOT" -f "labview_iso_path=$LABVIEW_ISO" -f "winrm_host_port=$port")"
  if [[ "$conclusion" != "success" ]]; then
    FAILURE_REASON="install-labview: $conclusion"; return 1
  fi

  conclusion="$(dispatch_and_wait register-runner \
    -f "vm_name=$vm_name" -f "storage_root=$STORAGE_ROOT" -f "winrm_host_port=$port" \
    -f "target_repo=$repo" -f "runner_name=$runner_name" -f "runner_labels=$runner_labels" \
    -f "dedicated_service_account_name=$service_account")"
  if [[ "$conclusion" != "success" ]]; then
    FAILURE_REASON="register-runner: $conclusion"; return 1
  fi

  return 0
}

FAILURES=()
READY=()
for vm_name in "${QUEUED[@]}"; do
  port="$(jq -r --arg vm "$vm_name" '.vms[] | select(.vmName == $vm) | .winrmHostPort' "$INVENTORY")"
  repo="$(jq -r --arg vm "$vm_name" '.vms[] | select(.vmName == $vm) | .targetRepo' "$INVENTORY")"
  runner_name="$(jq -r --arg vm "$vm_name" '.vms[] | select(.vmName == $vm) | .runnerName // ""' "$INVENTORY")"
  runner_labels="$(jq -r --arg vm "$vm_name" '.vms[] | select(.vmName == $vm) | .runnerLabels // ""' "$INVENTORY")"
  service_account="$(jq -r --arg vm "$vm_name" '.vms[] | select(.vmName == $vm) | .dedicatedServiceAccount // ""' "$INVENTORY")"
  guest_hostname="$(jq -r --arg vm "$vm_name" '.vms[] | select(.vmName == $vm) | .guestHostname // ""' "$INVENTORY")"
  guest_full_name="$(jq -r --arg vm "$vm_name" '.vms[] | select(.vmName == $vm) | .guestFullName // ""' "$INVENTORY")"
  if [[ -z "$guest_hostname" ]]; then
    guest_hostname="$(derive_guest_hostname "$vm_name")"
    log "$vm_name: no guestHostname in inventory, deriving a unique fallback: $guest_hostname"
  fi
  if [[ -z "$guest_full_name" ]]; then
    guest_full_name="VM Builder ($vm_name)"
  fi
  if [[ -z "$runner_name" || -z "$runner_labels" || -z "$service_account" ]]; then
    log "FAILED: $vm_name is missing runnerName/runnerLabels/dedicatedServiceAccount -- fill these in before queueing (register-runner needs them)."
    set_status "$vm_name" "failed" "queued without runnerName/runnerLabels/dedicatedServiceAccount"
    FAILURES+=("$vm_name")
    continue
  fi
  set_status "$vm_name" "provisioning"

  succeeded=0
  FAILURE_REASON=""
  for ((attempt = 1; attempt <= MAX_ATTEMPTS; attempt++)); do
    FAILURE_RETRYABLE=1
    log "=== Building $vm_name (attempt $attempt of $MAX_ATTEMPTS; target repo: $repo, WinRM port: $port) ==="
    if provision_vm_once "$vm_name" "$port" "$repo" "$runner_name" "$runner_labels" "$service_account" "$guest_hostname" "$guest_full_name"; then
      succeeded=1
      break
    fi
    log "$vm_name attempt $attempt of $MAX_ATTEMPTS FAILED ($FAILURE_REASON)"
    if [[ "$FAILURE_RETRYABLE" == "0" ]]; then
      log "$vm_name: preserving the VM because the guest credential was rejected; correct WINRM_PASSWORD_FILE before retrying."
      break
    fi
    if ! cleanup_vm_for_retry "$vm_name"; then
      FAILURE_REASON="$FAILURE_REASON; automatic poisoned-VM teardown failed"
      FAILURE_RETRYABLE=0
      break
    fi
    if (( attempt < MAX_ATTEMPTS )); then
      log "$vm_name: failed VM discarded; retrying from a fresh VM shell."
    fi
  done

  if [[ "$succeeded" != "1" ]]; then
    if [[ "$FAILURE_RETRYABLE" == "0" ]]; then
      log "FAILED: $vm_name stopped without another attempt (last failure: $FAILURE_REASON)"
      set_status "$vm_name" "failed" "$FAILURE_REASON"
    else
      log "FAILED: $vm_name exhausted all $MAX_ATTEMPTS attempts (last failure: $FAILURE_REASON); poisoned VM was discarded."
      set_status "$vm_name" "failed" "exhausted $MAX_ATTEMPTS attempts; last failure: $FAILURE_REASON; poisoned VM discarded"
    fi
    FAILURES+=("$vm_name")
    continue
  fi

  set_status "$vm_name" "ready-for-activation" "VIPM + LabVIEW installed and runner '$runner_name' registered $(date -u '+%Y-%m-%d'); awaiting operator activation only."
  READY+=("$vm_name")
  log "=== $vm_name is READY FOR ACTIVATION (port $port, repo $repo, runner $runner_name already registered) -- see README's 'Activate LabVIEW + VIPM' step. ==="
  if [[ "$DRY_RUN" != "1" ]]; then
    "$SCRIPT_DIR/vnc-console.sh" open "$vm_name" || log "WARNING: $vm_name: vnc-console.sh open failed -- open it manually (see README's 'Activate LabVIEW + VIPM' step)."
  fi
done

log "Batch complete. Ready for activation: ${READY[*]:-none}. Failed: ${FAILURES[*]:-none}."
[[ ${#FAILURES[@]} -eq 0 ]]

