#!/usr/bin/env bash
set -euo pipefail

# Builds the auxiliary ISO that carries the NSIS WinRM-setup installer onto
# a fresh Windows guest (issue #200, epic #198). This is delivery
# infrastructure only -- per epic #198's architecture principle ("NSIS shall
# be leveraged for injection points"), this ISO must never carry anything
# beyond compiled NSIS installer(s); no scripts, batch files, or other logic
# that bypasses NSIS belongs here.
#
# Mirrors the already-proven pattern used for the VIPM installer ISO
# (.github/workflows/provision-windows-labview-vm.yml's "Build VIPM
# Installer ISO" step: `xorriso -as mkisofs -V VIPMSETUP -J -r`), with one
# addition this pattern doesn't need: the NSIS installer isn't a pre-built,
# operator-supplied .exe like VIPM's -- it's compiled from tracked source
# (winrm-setup-installer.nsi) via makensis, every time this script runs, so
# the ISO's contents always match the current source rather than a stale
# prebuilt binary (the compiled .exe is gitignored repo-wide and must never
# be committed).
#
# Usage:
#   ./build-nsis-bootstrap-iso.sh [--out PATH]
#
# Prints the resulting ISO's absolute path on stdout as its last line (and
# nothing else on stdout), so callers can capture it directly, e.g.:
#   iso_path="$(./build-nsis-bootstrap-iso.sh)"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NSI_SOURCE="$SCRIPT_DIR/winrm-setup-installer.nsi"
VOLUME_LABEL="SBOMNSISAUX"
OUT=""

usage() {
  cat <<'EOF'
Usage: ./build-nsis-bootstrap-iso.sh [--out PATH]

Compiles winrm-setup-installer.nsi with makensis and wraps the resulting
.exe into a small ISO (volume label SBOMNSISAUX), so it can be attached to
a VM at creation time and read by the guest before WinRM (or any other
guest-automation channel) exists. Prints the ISO's absolute path on stdout.
EOF
}

while (($#)); do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

command -v makensis >/dev/null || { echo "makensis is required (apt install nsis)" >&2; exit 2; }
command -v xorriso >/dev/null || { echo "xorriso is required" >&2; exit 2; }
[[ -r "$NSI_SOURCE" ]] || { echo "NSIS installer source missing: $NSI_SOURCE" >&2; exit 2; }

work_dir="$(mktemp -d)"
trap 'rm -rf "$work_dir"' EXIT

cp "$NSI_SOURCE" "$work_dir/"
( cd "$work_dir" && makensis -V2 "$(basename "$NSI_SOURCE")" ) >&2

compiled_exe="$work_dir/winrm-setup-installer.exe"
[[ -f "$compiled_exe" ]] || { echo "makensis did not produce winrm-setup-installer.exe" >&2; exit 3; }

iso_root="$work_dir/iso-root"
mkdir -p "$iso_root"
cp "$compiled_exe" "$iso_root/"

[[ -n "$OUT" ]] || OUT="$(mktemp --suffix=.iso)"

xorriso -as mkisofs -o "$OUT" -V "$VOLUME_LABEL" -J -r "$iso_root/" >&2

echo "$OUT"
