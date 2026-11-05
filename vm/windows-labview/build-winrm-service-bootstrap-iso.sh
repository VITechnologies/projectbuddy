#!/usr/bin/env bash
set -euo pipefail

# Builds the auxiliary ISO that carries the Windows-Service-based WinRM
# bootstrap installer onto a fresh Windows guest -- a research prototype
# testing issue #191's own recommended next step (see
# winrm-service-bootstrap-installer.nsi's own header comment for the full
# rationale: a genuine SCM service, registered the same way the
# already-proven-safe wuauserv/UsoSvc StartupType disable is, as an
# alternative to the RunOnce/FirstLogonCommands mechanisms #191 already
# found do not survive the #188 restart).
#
# Mirrors build-nsis-bootstrap-iso.sh's own pattern exactly, with one
# addition: this installer packages NSSM (the service-wrapper binary NSIS
# itself cannot produce, since NSIS can't implement a native
# StartServiceCtrlDispatcher control handler) as an embedded resource, so
# this script downloads + SHA-256-verifies it against
# provisioning-manifest.json's pinned "nssm" entry before compiling --
# nssm.exe itself is never committed to this repository.
#
# Usage:
#   ./build-winrm-service-bootstrap-iso.sh [--out PATH] [--nssm-cache DIR]
#
# Prints the resulting ISO's absolute path on stdout as its last line (and
# nothing else on stdout), so callers can capture it directly, e.g.:
#   iso_path="$(./build-winrm-service-bootstrap-iso.sh)"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NSI_SOURCE="$SCRIPT_DIR/winrm-service-bootstrap-installer.nsi"
SERVICE_SCRIPT_SOURCE="$SCRIPT_DIR/winrm-setup-service.ps1"
MANIFEST="$SCRIPT_DIR/provisioning-manifest.json"
VOLUME_LABEL="SBOMSVCAUX"
OUT=""
NSSM_CACHE_DIR=""
EXE_OUT=""

usage() {
  cat <<'EOF'
Usage: ./build-winrm-service-bootstrap-iso.sh [--out PATH] [--nssm-cache DIR] [--exe-out PATH]

Downloads and SHA-256-verifies NSSM (per provisioning-manifest.json's
pinned "nssm" entry), compiles winrm-service-bootstrap-installer.nsi with
makensis (embedding NSSM and winrm-setup-service.ps1 as packaged
resources), and wraps the resulting .exe into a small ISO (volume label
SBOMSVCAUX). --nssm-cache reuses a previously-downloaded+verified zip
instead of re-downloading every run. --exe-out additionally copies the
compiled installer .exe itself (e.g. for vagrant-service-test/'s
upload-installer provisioner, which doesn't need the ISO wrapping).
Prints the ISO's absolute path on stdout.
EOF
}

while (($#)); do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
    --nssm-cache) NSSM_CACHE_DIR="$2"; shift 2 ;;
    --exe-out) EXE_OUT="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

command -v makensis >/dev/null || { echo "makensis is required (apt install nsis)" >&2; exit 2; }
command -v xorriso >/dev/null || { echo "xorriso is required" >&2; exit 2; }
command -v curl >/dev/null || { echo "curl is required" >&2; exit 2; }
[[ -r "$NSI_SOURCE" ]] || { echo "NSIS installer source missing: $NSI_SOURCE" >&2; exit 2; }
[[ -r "$SERVICE_SCRIPT_SOURCE" ]] || { echo "Service script missing: $SERVICE_SCRIPT_SOURCE" >&2; exit 2; }
[[ -r "$MANIFEST" ]] || { echo "Manifest missing: $MANIFEST" >&2; exit 2; }

read -r nssm_url nssm_zip_sha256 nssm_exe_path_in_zip nssm_exe_sha256 < <(
  python3 -c "
import json
with open('$MANIFEST') as f:
    m = json.load(f)['nssm']
print(m['downloadUrl'], m['zipSha256'], m['win64ExePathInZip'], m['win64ExeSha256'])
"
)

work_dir="$(mktemp -d)"
trap 'rm -rf "$work_dir"' EXIT

if [[ -n "$NSSM_CACHE_DIR" ]]; then
  mkdir -p "$NSSM_CACHE_DIR"
  nssm_zip="$NSSM_CACHE_DIR/$(basename "$nssm_url")"
else
  nssm_zip="$work_dir/$(basename "$nssm_url")"
fi

if [[ ! -f "$nssm_zip" ]]; then
  curl -fsSL -o "$nssm_zip" "$nssm_url"
fi

actual_zip_sha256="$(sha256sum "$nssm_zip" | cut -d' ' -f1)"
[[ "$actual_zip_sha256" == "$nssm_zip_sha256" ]] || {
  echo "NSSM zip SHA-256 mismatch: expected $nssm_zip_sha256, got $actual_zip_sha256" >&2
  exit 3
}

unzip -o -q "$nssm_zip" "$nssm_exe_path_in_zip" -d "$work_dir/nssm-extract"
nssm_exe="$work_dir/nssm-extract/$nssm_exe_path_in_zip"
[[ -f "$nssm_exe" ]] || { echo "NSSM exe not found in zip at expected path: $nssm_exe_path_in_zip" >&2; exit 3; }

actual_exe_sha256="$(sha256sum "$nssm_exe" | cut -d' ' -f1)"
[[ "$actual_exe_sha256" == "$nssm_exe_sha256" ]] || {
  echo "NSSM exe SHA-256 mismatch: expected $nssm_exe_sha256, got $actual_exe_sha256" >&2
  exit 3
}

cp "$NSI_SOURCE" "$work_dir/"
cp "$SERVICE_SCRIPT_SOURCE" "$work_dir/"
cp "$nssm_exe" "$work_dir/nssm.exe"

( cd "$work_dir" && makensis -V2 "$(basename "$NSI_SOURCE")" ) >&2

compiled_exe="$work_dir/winrm-service-bootstrap-installer.exe"
[[ -f "$compiled_exe" ]] || { echo "makensis did not produce winrm-service-bootstrap-installer.exe" >&2; exit 3; }

if [[ -n "$EXE_OUT" ]]; then
  cp "$compiled_exe" "$EXE_OUT"
fi

iso_root="$work_dir/iso-root"
mkdir -p "$iso_root"
cp "$compiled_exe" "$iso_root/"

[[ -n "$OUT" ]] || OUT="$(mktemp --suffix=.iso)"

xorriso -as mkisofs -o "$OUT" -V "$VOLUME_LABEL" -J -r "$iso_root/" >&2

echo "$OUT"
