#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_MANIFEST="$SCRIPT_DIR/provisioning-manifest.json"

usage() {
  cat <<'EOF'
Usage:
  sudo ./provision-guest.sh [--manifest <path>] [--primary-user <name>]
  ./provision-guest.sh --validate-manifest [path]

Installs only public LabVIEW/VIPM prerequisites. LabVIEW activation remains an
operator step; use activate-vipm.sh separately for env-file VIPM activation.
EOF
}

validate_manifest() {
  python3 - "$1" <<'PY'
import json
import pathlib
import re
import sys
from urllib.parse import urlparse

path = pathlib.Path(sys.argv[1])
manifest = json.loads(path.read_text(encoding="utf-8"))
errors = []

def require(condition, message):
    if not condition:
        errors.append(message)

def value(*keys):
    current = manifest
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            errors.append(f"missing manifest field: {'.'.join(keys)}")
            return None
        current = current[key]
    return current

require(manifest.get("schema") == "sandbox-sbom-reporting/ubuntu-labview-provisioning@1", "unsupported manifest schema")
require(value("os", "distribution") == "ubuntu", "guest distribution must be ubuntu")
require(value("os", "version") == "24.04", "guest version must be 24.04")
require(value("os", "codename") == "noble", "guest codename must be noble")
require(value("labview", "edition") == "pro", "LabVIEW edition must be pro")
require(value("labview", "year") == "2026", "LabVIEW year must be 2026")
require(value("labview", "quarter") == "Q3", "LabVIEW quarter must be Q3")
require(value("labview", "package") == "ni-labview-2026-pro", "unexpected LabVIEW package")
require(value("labview", "packageVersion") == "26.3.1.49165-0+f13", "unexpected LabVIEW package version")
require(value("labview", "executableBasenames") == ["labview", "labviewprofull"], "unexpected LabVIEW executable basenames")
require(value("labview", "viServerPort") == 3363, "VI Server port must be 3363")
require(value("labview", "repositoryKey") == "ni-signing-authority.asc", "unexpected NI signing key file")
require(value("labview", "repositoryKeyFingerprint") == "D1DD926720DF53BD5C442FABA83F33D0666E7520", "unexpected NI key fingerprint")
require(value("vipm", "version") == "26.3.2-4056", "unexpected VIPM release")
require(value("vipm", "sha256") == "89f50b8dc7ac023224f1b8f822ff99ac221ad31c67bae06951c8d1e09e7eef6c", "unexpected VIPM SHA-256")
require(str(value("labview", "repository") or "").endswith("/LabVIEW/2026/Q3/f1/pro/deb/ni-labview-2026/noble"), "unexpected LabVIEW repository")
require(str(value("vipm", "url") or "").endswith("/vipm_26.3.2-4056_amd64.deb"), "VIPM URL does not match the pinned release")

for field in (("labview", "repository"), ("vipm", "url")):
    parsed = urlparse(str(value(*field) or ""))
    require(parsed.scheme == "https" and bool(parsed.netloc), f"{'.'.join(field)} must be HTTPS")

for text_field in (
    ("labview", "repositoryDistribution"),
    ("labview", "repositoryComponent"),
    ("labview", "repositoryKey"),
    ("labview", "packageVersion"),
    ("labview", "installPath"),
):
    item = value(*text_field)
    require(isinstance(item, str) and bool(item) and not re.search(r"[\t\r\n]", item), f"invalid {'.'.join(text_field)}")

forbidden = re.compile(r"password|serial|token|credential|private.?key", re.IGNORECASE)
def inspect_keys(item, prefix=""):
    if isinstance(item, dict):
        for key, child in item.items():
            if forbidden.search(key):
                errors.append(f"secret-bearing field is forbidden: {prefix}{key}")
            inspect_keys(child, f"{prefix}{key}.")
    elif isinstance(item, list):
        for child in item:
            inspect_keys(child, prefix)
inspect_keys(manifest)

if errors:
    for error in dict.fromkeys(errors):
        print(f"manifest contract: FAIL: {error}", file=sys.stderr)
    raise SystemExit(2)
print("manifest contract: PASS")
PY
}

manifest="$DEFAULT_MANIFEST"
primary_user="${SUDO_USER:-${USER:-actor}}"
validate_only=0

while (($#)); do
  case "$1" in
    --manifest)
      [[ $# -ge 2 ]] || { echo "--manifest requires a path" >&2; exit 2; }
      manifest="$2"
      shift 2
      ;;
    --primary-user)
      [[ $# -ge 2 ]] || { echo "--primary-user requires a name" >&2; exit 2; }
      primary_user="$2"
      shift 2
      ;;
    --validate-manifest)
      validate_only=1
      if [[ $# -ge 2 && "$2" != --* ]]; then manifest="$2"; shift; fi
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

[[ -r "$manifest" ]] || { echo "manifest not readable: $manifest" >&2; exit 2; }
validate_manifest "$manifest"
((validate_only)) && exit 0
[[ $EUID -eq 0 ]] || { echo "run provisioning as root" >&2; exit 1; }

IFS=$'\t' read -r os_version os_codename ni_repo ni_distribution ni_component ni_key_file ni_fingerprint \
  labview_package labview_version install_path executable_names vi_server_port vipm_version vipm_url vipm_sha256 < <(
  python3 - "$manifest" <<'PY'
import json
import sys
manifest = json.load(open(sys.argv[1], encoding="utf-8"))
labview = manifest["labview"]
vipm = manifest["vipm"]
print("\t".join((
    manifest["os"]["version"], manifest["os"]["codename"], labview["repository"],
    labview["repositoryDistribution"], labview["repositoryComponent"], labview["repositoryKey"],
    labview["repositoryKeyFingerprint"], labview["package"], labview["packageVersion"],
    labview["installPath"], ",".join(labview["executableBasenames"]), str(labview["viServerPort"]),
    vipm["version"], vipm["url"], vipm["sha256"],
)))
PY
)

# shellcheck source=/etc/os-release disable=SC1091
source /etc/os-release
[[ "${ID:-}" == ubuntu && "${VERSION_ID:-}" == "$os_version" && "${VERSION_CODENAME:-}" == "$os_codename" ]] || {
  echo "unsupported guest OS: expected Ubuntu $os_version ($os_codename), found ${PRETTY_NAME:-unknown}" >&2
  exit 3
}
id "$primary_user" >/dev/null 2>&1 || { echo "primary user does not exist: $primary_user" >&2; exit 3; }

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends acl ca-certificates curl gnupg python3 xvfb x11-utils

key_source="$SCRIPT_DIR/$ni_key_file"
[[ -r "$key_source" ]] || { echo "NI signing key not found: $key_source" >&2; exit 3; }
actual_fingerprint="$(gpg --batch --show-keys --with-colons "$key_source" | awk -F: '$1 == "fpr" { print $10; exit }')"
[[ "$actual_fingerprint" == "$ni_fingerprint" ]] || {
  echo "NI signing key fingerprint mismatch: expected $ni_fingerprint, found ${actual_fingerprint:-none}" >&2
  exit 4
}

key_destination="/usr/share/keyrings/sandbox-ni-labview.asc"
install -m 0644 "$key_source" "$key_destination"
temporary_dir="$(mktemp -d)"
signature_keyring="$temporary_dir/ni-signing-authority.gpg"
inrelease="$temporary_dir/InRelease"
vipm_deb="$temporary_dir/vipm.deb"
trap 'rm -rf "$temporary_dir"' EXIT
gpg --batch --dearmor --output "$signature_keyring" "$key_source"
curl -fsSL "$ni_repo/dists/$ni_distribution/InRelease" -o "$inrelease"
gpgv --keyring "$signature_keyring" "$inrelease"

source_list="/etc/apt/sources.list.d/sandbox-ni-labview.list"
printf 'deb [signed-by=%s] %s %s %s\n' "$key_destination" "$ni_repo" "$ni_distribution" "$ni_component" > "$source_list"
apt-get update
apt-get install -y "$labview_package=$labview_version"
installed_labview="$(dpkg-query -W -f='${Version}' "$labview_package")"
[[ "$installed_labview" == "$labview_version" ]] || {
  echo "LabVIEW package version mismatch: expected $labview_version, found $installed_labview" >&2
  exit 5
}

curl -fsSL "$vipm_url" -o "$vipm_deb"
printf '%s  %s\n' "$vipm_sha256" "$vipm_deb" | sha256sum --check
apt-get install -y "$vipm_deb"
installed_vipm="$(dpkg-query -W -f='${Version}' vipm)"
[[ "$installed_vipm" == "$vipm_version" ]] || {
  echo "VIPM package version mismatch: expected $vipm_version, found $installed_vipm" >&2
  exit 6
}

user_home="$(getent passwd "$primary_user" | cut -d: -f6)"
config_dir="$user_home/natinst/.config/LabVIEW-2026"
install -d -m 0755 -o "$primary_user" -g "$primary_user" "$config_dir"
IFS=',' read -r -a basenames <<< "$executable_names"
for basename in "${basenames[@]}"; do
  executable="$install_path/$basename"
  [[ -x "$executable" ]] || { echo "LabVIEW executable missing: $executable" >&2; exit 7; }
  cat > "$config_dir/$basename.conf" <<EOF
[LabVIEW]
server.tcp.enabled=TRUE
server.tcp.port=$vi_server_port
server.tcp.serviceName=""
server.tcp.access="+*"
server.vi.access="+*"
EOF
  chown "$primary_user:$primary_user" "$config_dir/$basename.conf"
  chmod 0644 "$config_dir/$basename.conf"
done

# VIPM runs as the dedicated build user, while NI's Debian packages install the
# LabVIEW tree as root. Preserve root ownership and mode bits, but grant that one
# account inherited package-management access throughout the pinned install.
setfacl -R -m "u:$primary_user:rwX" "$install_path"
find "$install_path" -type d -exec setfacl -m "d:u:$primary_user:rwx" {} +
[[ "$(stat -c '%U:%G' "$install_path")" == root:root ]] || {
  echo "LabVIEW install ownership changed while applying package-build ACLs" >&2
  exit 8
}
for package_root in vi.lib user.lib examples menus; do
  runuser -u "$primary_user" -- test -w "$install_path/$package_root" || {
    echo "LabVIEW package root is not writable by $primary_user: $package_root" >&2
    exit 8
  }
done

receipt_dir=/var/lib/sandbox-sbom-reporting
base_state="$receipt_dir/base-bootstrap-state.json"
receipt="$receipt_dir/public-provisioning-receipt.json"
[[ -r "$base_state" ]] || { echo "base bootstrap identity state is missing" >&2; exit 8; }
manifest_sha256="$(sha256sum "$manifest" | awk '{print $1}')"
python3 - "$base_state" "$receipt" "$manifest_sha256" \
  "$os_version" "$os_codename" "$labview_package" "$labview_version" "$install_path" \
  "$executable_names" "$vi_server_port" "$vipm_version" <<'PY'
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

(
    state_path, destination, manifest_sha256, os_version, os_codename,
    labview_package, labview_version, install_path, executable_names,
    vi_server_port, vipm_version,
) = sys.argv[1:]
with open(state_path, encoding="utf-8") as handle:
    state = json.load(handle)
receipt = {
    "schema": "sandbox-sbom-reporting/ubuntu-public-provisioning@1",
    "manifestSha256": manifest_sha256,
    "vm": state["vm"],
    "os": {"distribution": "ubuntu", "version": os_version, "codename": os_codename},
    "labview": {
      "edition": "pro",
      "package": labview_package,
      "packageVersion": labview_version,
      "installPath": install_path,
      "executableBasenames": executable_names.split(","),
      "viServerPort": int(vi_server_port),
    },
    "vipm": {"package": "vipm", "packageVersion": vipm_version},
    "verifiedAt": datetime.now(timezone.utc).isoformat(),
    "failures": [],
    "outcome": "PASS",
}
os.makedirs(os.path.dirname(destination), mode=0o755, exist_ok=True)
descriptor, temporary = tempfile.mkstemp(prefix=".public-provisioning-", dir=os.path.dirname(destination))
try:
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(receipt, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.chmod(temporary, 0o644)
    os.replace(temporary, destination)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY

echo "public provisioning complete: LabVIEW $labview_version Pro; VIPM $vipm_version"
echo "public provisioning receipt: $receipt"
echo "reboot once, activate LabVIEW in the VM console, then run activate-vipm.sh with a mode-600 env file"