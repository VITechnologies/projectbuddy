#!/usr/bin/env bash
# Opens (or closes) an interactive VNC console to an Ubuntu LabVIEW VM for
# the one step that is deliberately never automated: LabVIEW's own
# interactive activation (see docs/vm-provisioning/procedure.md's
# "complete-activation" phase description -- VIPM activation IS automated
# via activate-vipm.sh's `vipm activate --direct-vipm-io`, but LabVIEW
# itself requires a human in its own activation dialog). Ported from
# vm/windows-labview/vnc-console.sh's proven pattern (that family's own
# "Activate LabVIEW + VIPM" runbook step, issue #176) -- this family had no
# equivalent console-access tooling at all before this (#62).
#
# This VirtualBox build's VRDE module is VBoxVNC (the open-source
# VNC-protocol server), NOT Oracle's RDP-protocol extension -- an RDP
# client (e.g. xfreerdp, Windows' own Remote Desktop Connection) cannot
# connect to it at all. Everything here speaks VNC.
#
# "open" enables VRDE on a loopback-only port, sets a fresh random
# password, records the session in a single human-readable status file
# (so the password is recoverable if a popped-up window is closed), and
# launches Remmina pointed at it automatically if a local GUI display is
# available (unless --no-launch). If this script is run over a plain SSH
# session with no DISPLAY (the expected case when operating this engine's
# `vm-provisioner` host remotely): either reconnect with X11 forwarding
# (`ssh -X`) so Remmina can still launch and render back to your own
# screen, or skip that and instead tunnel the loopback-bound VRDE port out
# over SSH port-forwarding (`ssh -L <local-port>:127.0.0.1:<port>
# <this-host>`) and point your own native VNC client (not Remmina
# necessarily -- any VNC client works) at `localhost:<local-port>`. Either
# way, no part of this needs a GUI session on the provisioning host
# itself.
#
# "close" turns VRDE back off and removes the session record.
#
# Usage:
#   ./vnc-console.sh open <vm-name> [--no-launch] [--port N]
#   ./vnc-console.sh close <vm-name>
#   ./vnc-console.sh list
set -euo pipefail

SESSIONS_FILE="${XDG_RUNTIME_DIR:-/tmp}/vm-vnc-sessions-ubuntu.txt"
NO_LAUNCH=0
EXPLICIT_PORT=""

usage() {
  cat >&2 << 'USAGE'
Usage:
  vnc-console.sh open <vm-name> [--no-launch] [--port N]
  vnc-console.sh close <vm-name>
  vnc-console.sh list
USAGE
}

command -v VBoxManage >/dev/null || { echo "VBoxManage is required" >&2; exit 2; }

ACTION="${1:-}"; shift || true

case "$ACTION" in
  list)
    if [[ -s "$SESSIONS_FILE" ]]; then
      printf 'VM\tADDRESS\tPASSWORD\tOPENED_AT\n'
      cat "$SESSIONS_FILE"
    else
      echo "No active VNC sessions recorded in $SESSIONS_FILE." >&2
    fi
    exit 0
    ;;
  open|close)
    ;;
  *)
    usage
    exit 2
    ;;
esac

VM_NAME="${1:-}"; shift || true
[[ -n "$VM_NAME" ]] || { usage; exit 2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-launch) NO_LAUNCH=1; shift ;;
    --port) EXPLICIT_PORT="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

remove_session_record() {
  [[ -f "$SESSIONS_FILE" ]] || return 0
  local tmp; tmp="$(mktemp)"
  grep -v -P "^${VM_NAME}\t" "$SESSIONS_FILE" > "$tmp" || true # empty result is fine
  mv "$tmp" "$SESSIONS_FILE"
}

if [[ "$ACTION" == "close" ]]; then
  VBoxManage controlvm "$VM_NAME" vrde off
  remove_session_record
  echo "VRDE disabled for $VM_NAME; session record cleared." >&2
  exit 0
fi

# --- open ---
state="$(VBoxManage showvminfo "$VM_NAME" --machinereadable 2>/dev/null | sed -n 's/^VMState="\(.*\)"$/\1/p')"
[[ "$state" == "running" ]] || { echo "ERROR: $VM_NAME is not running (state: ${state:-not registered})." >&2; exit 1; }

# No persistent per-VM inventory file for this family (unlike Windows'
# runner-inventory.json) -- this is a disposable-appliance lifecycle, not
# a long-lived fleet. Scan forward from a sensible default so concurrent
# sessions (including a Windows VM already on the same default) don't
# collide, same as Windows' own fallback path.
port="$EXPLICIT_PORT"
if [[ -z "$port" ]]; then
  port=3390
  while (echo > "/dev/tcp/127.0.0.1/$port") 2>/dev/null; do
    echo "NOTE: port $port is already in use by another listener; trying $((port + 1))." >&2
    port=$((port + 1))
  done
fi

password="$(openssl rand -base64 9 | tr -d '/+=' | head -c 12)"

# Confirmed live 2026-11-04 on the Windows family (equally applicable
# here -- the failure mode is in VirtualBox's own VRDE enable path, not
# anything guest-OS-specific): enabling VRDE on a running VM can fail
# transiently with "VBoxManage: error: Code NS_ERROR_ABORT ... Context:
# COMSETTER(Enabled)(fEnabled)" -- and in the observed case this didn't
# just fail cleanly, it crashed the entire VM to the "aborted" state.
# Retry the enable sequence a few times for the ordinary transient case,
# and if the VM is actually down afterward, restart it and retry once
# more rather than leaving a crashed, offline VM behind.
vm_state() {
  VBoxManage showvminfo "$VM_NAME" --machinereadable 2>/dev/null | sed -n 's/^VMState="\(.*\)"$/\1/p'
}

enable_vrde() {
  local attempt
  for attempt in 1 2 3; do
    if VBoxManage controlvm "$VM_NAME" vrde on \
      && VBoxManage controlvm "$VM_NAME" vrdeproperty "TCP/Address=127.0.0.1" \
      && VBoxManage controlvm "$VM_NAME" vrdeport "$port" \
      && VBoxManage controlvm "$VM_NAME" vrdeproperty "VNCPassword=$password"; then
      return 0
    fi
    echo "NOTE: enabling VRDE for $VM_NAME failed (attempt $attempt/3); retrying in 5s..." >&2
    sleep 5
  done
  return 1
}

if ! enable_vrde; then
  post_state="$(vm_state)"
  if [[ "$post_state" != "running" ]]; then
    echo "WARNING: $VM_NAME crashed (state: ${post_state:-unknown}) while enabling VRDE -- a known intermittent VBoxManage issue (NS_ERROR_ABORT on COMSETTER(Enabled)). Restarting it and retrying once..." >&2
    VBoxManage startvm "$VM_NAME" --type headless
    sleep 15
    if ! enable_vrde; then
      echo "ERROR: $VM_NAME was restarted but VRDE still could not be enabled. Connect manually once the guest has fully booted." >&2
      exit 1
    fi
    echo "NOTE: $VM_NAME recovered after a restart; VRDE is now enabled." >&2
  else
    echo "ERROR: could not enable VRDE for $VM_NAME after 3 attempts (VM still running; not a crash this time)." >&2
    exit 1
  fi
fi

# Rewrite (not append) this VM's line so repeated opens don't accumulate
# stale entries -- one current line per VM.
tmp_sessions="$(mktemp)"
{
  if [[ -f "$SESSIONS_FILE" ]]; then
    grep -v -P "^${VM_NAME}\t" "$SESSIONS_FILE" || true
  fi
  printf '%s\t127.0.0.1:%s\t%s\t%s\n' "$VM_NAME" "$port" "$password" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
} > "$tmp_sessions"
mv "$tmp_sessions" "$SESSIONS_FILE"
chmod 600 "$SESSIONS_FILE"

cat >&2 << SUMMARY
=== VNC console: $VM_NAME ===
Address:  127.0.0.1:$port
Password: $password
Recorded in: $SESSIONS_FILE (run "$0 list" or "cat $SESSIONS_FILE" any time
to recover this if the popped-up window is closed. Run
"$0 close $VM_NAME" when done.)

Connecting remotely (no DISPLAY on this host, e.g. a plain SSH session to
the vm-provisioner host): either reconnect with "ssh -X" so Remmina can
launch here and render back to your own screen, or tunnel instead:
  ssh -L ${port}:127.0.0.1:${port} <this-host>
then point any VNC client (not necessarily Remmina) at 127.0.0.1:${port}
with the password above.
SUMMARY

if [[ "$NO_LAUNCH" == "1" ]]; then
  echo "NOTE: --no-launch set; not opening a GUI window." >&2
  exit 0
fi
if [[ -z "${DISPLAY:-}" && -z "${WAYLAND_DISPLAY:-}" ]]; then
  echo "NOTE: no DISPLAY/WAYLAND_DISPLAY detected; not opening a GUI window. Connect per the instructions above." >&2
  exit 0
fi
if ! command -v remmina >/dev/null; then
  echo "NOTE: remmina not found on PATH; connect manually with the details above." >&2
  exit 0
fi

# Detached so this script (and any caller) doesn't block waiting for the
# window to close.
nohup remmina -c "vnc://127.0.0.1:${port}" >/dev/null 2>&1 &
disown
echo "Launched Remmina for $VM_NAME (it will prompt for the password above)." >&2
