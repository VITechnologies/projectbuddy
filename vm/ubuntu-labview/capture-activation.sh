#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT_DIR=
VIPM_ENV=
LABVIEW_PATH=/usr/local/natinst/LabVIEW-2026-64/labview
PROBE_VI=/usr/local/natinst/share/nilvcli/Examples/AddTwoNumbers/AddTwoNumbers.vi
LABVIEW_CLI=/usr/local/bin/LabVIEWCLI

usage() {
    echo "usage: capture-activation.sh --output-dir <path> [--vipm-env <mode-600-env-file>]"
}

while (($#)); do
    case "$1" in
        --output-dir) OUTPUT_DIR="${2:-}"; shift 2 ;;
        --vipm-env) VIPM_ENV="${2:-}"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "activation capture: FAIL: unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done
[[ -n "$OUTPUT_DIR" ]] || { echo "activation capture: FAIL: --output-dir is required" >&2; exit 2; }

temporary_dir="$(mktemp -d)"
probe_output="$temporary_dir/probe-output.txt"
cleanup() {
    rm -rf "$temporary_dir"
    if [[ -n "$VIPM_ENV" ]]; then rm -f "$VIPM_ENV"; fi
}
trap cleanup EXIT

if [[ -n "$VIPM_ENV" ]]; then
    "$SCRIPT_DIR/activate-vipm.sh" "$VIPM_ENV"
fi
for path in "$LABVIEW_PATH" "$PROBE_VI" "$LABVIEW_CLI"; do
    [[ -e "$path" ]] || { echo "activation capture: FAIL: required path is missing: $path" >&2; exit 2; }
done
command -v xvfb-run >/dev/null 2>&1 || { echo "activation capture: FAIL: xvfb-run is missing" >&2; exit 127; }
command -v vipm >/dev/null 2>&1 || { echo "activation capture: FAIL: vipm is missing" >&2; exit 127; }

started_ns="$(date +%s%N)"
set +e
xvfb-run -a timeout 240 "$LABVIEW_CLI" -LabVIEWPath "$LABVIEW_PATH" -PortNumber 3363 \
    -OperationName RunVI -VIPath "$PROBE_VI" 20 22 >"$probe_output" 2>&1
probe_exit=$?
set -e
ended_ns="$(date +%s%N)"
wall_ms=$(( (ended_ns - started_ns) / 1000000 ))

python3 - "$probe_output" "$temporary_dir/activation-known-answer-capture.json" "$probe_exit" "$wall_ms" <<'PY'
import json
import pathlib
import platform
import socket
import sys

probe_output, destination, exit_code, wall_ms = sys.argv[1:]
output = pathlib.Path(probe_output).read_text(encoding="utf-8", errors="replace")
record = {
    "schema": "sandbox-sbom-reporting/labview-known-answer-capture@1",
    "probeVi": "/usr/local/natinst/share/nilvcli/Examples/AddTwoNumbers/AddTwoNumbers.vi",
    "labviewPath": "/usr/local/natinst/LabVIEW-2026-64/labview",
    "inputs": [20, 22],
    "expectedOutput": 42,
    "exitCode": int(exit_code),
    "wallMs": int(wall_ms),
    "output": output,
    "displayMode": "xvfb",
    "host": {
        "os": platform.system().lower(),
        "hostname": socket.gethostname(),
        "bootId": pathlib.Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip(),
    },
}
pathlib.Path(destination).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
PY
if ((probe_exit != 0)); then
    echo "activation capture: FAIL: LabVIEW known-answer probe failed; probe output withheld" >&2
    exit 1
fi

vipm --json about >"$temporary_dir/sandbox-vipm-about.json"
vipm --json labview list >"$temporary_dir/sandbox-vipm-labview.json"
vipm --json --labview-version 2026 --labview-bitness 64 list --installed >"$temporary_dir/sandbox-vipm-installed.json"
python3 - <<'PY' >"$temporary_dir/sandbox-runtime-state.json"
import json
import pathlib
import platform
import subprocess

os_release = {}
for line in pathlib.Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
    if "=" in line:
        key, value = line.split("=", 1)
        os_release[key] = value.strip('"')
gdm = pathlib.Path("/etc/gdm3/custom.conf").read_text(encoding="utf-8")
autologin = any(
    line.strip() == "AutomaticLoginEnable = true"
    for line in gdm.splitlines()
    if not line.lstrip().startswith("#")
)
print(json.dumps({
    "bootId": pathlib.Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip(),
    "hostname": platform.node(),
    "os": {
        "distribution": os_release.get("ID"),
        "version": os_release.get("VERSION_ID"),
        "codename": os_release.get("VERSION_CODENAME"),
    },
    "automaticLoginEnabled": autologin,
    "labviewPackageVersion": subprocess.check_output(
        ["dpkg-query", "-W", "-f=${Version}", "ni-labview-2026-pro"], text=True
    ),
}, indent=2))
PY

mkdir -p "$OUTPUT_DIR"
for name in activation-known-answer-capture.json sandbox-runtime-state.json \
    sandbox-vipm-about.json sandbox-vipm-labview.json sandbox-vipm-installed.json; do
    install -m 600 "$temporary_dir/$name" "$OUTPUT_DIR/$name"
done
echo "activation capture: PASS: five non-secret captures written to $OUTPUT_DIR"