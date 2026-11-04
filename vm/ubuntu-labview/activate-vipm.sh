#!/usr/bin/env bash
set -euo pipefail

env_file="${1:-.env}"
if [[ ! -f "$env_file" ]]; then
    echo "VIPM activation: FAIL: env file does not exist" >&2
    exit 2
fi
if [[ "$(stat -c '%a' "$env_file")" != 600 ]]; then
    echo "VIPM activation: FAIL: env file must have mode 600" >&2
    exit 2
fi

VIPM_SERIAL_NUMBER=
VIPM_FULL_NAME=
VIPM_EMAIL=
while IFS='=' read -r key value || [[ -n "$key" ]]; do
    key="${key%$'\r'}"
    value="${value%$'\r'}"
    case "$key" in
        VIPM_SERIAL_NUMBER) VIPM_SERIAL_NUMBER="$value" ;;
        VIPM_FULL_NAME) VIPM_FULL_NAME="$value" ;;
        VIPM_EMAIL) VIPM_EMAIL="$value" ;;
        VIPM_PASSWORD)
            echo "VIPM activation: FAIL: VIPM_PASSWORD is not used by vipm activate; remove it from this env file" >&2
            exit 2
            ;;
        ''|'#'*) ;;
    esac
done < "$env_file"

for name in VIPM_SERIAL_NUMBER VIPM_FULL_NAME VIPM_EMAIL; do
    if [[ -z "${!name}" ]]; then
        echo "VIPM activation: FAIL: $name is missing" >&2
        exit 2
    fi
done
if ! command -v vipm >/dev/null 2>&1; then
    echo "VIPM activation: FAIL: vipm is not installed" >&2
    exit 127
fi

activation_log="$(mktemp)"
about_json="$(mktemp)"
chmod 600 "$activation_log" "$about_json"
trap 'rm -f "$activation_log" "$about_json"' EXIT

echo "VIPM activation: invoking VIPM 26.3.2 direct activation"
if ! VIPM_NONINTERACTIVE=1 vipm activate --direct-vipm-io \
    --serial-number "$VIPM_SERIAL_NUMBER" \
    --name "$VIPM_FULL_NAME" \
    --email "$VIPM_EMAIL" >"$activation_log" 2>&1; then
    echo "VIPM activation: FAIL: vipm activate failed; output withheld because it may contain credential data" >&2
    exit 1
fi

if ! vipm --json about >"$about_json" 2>/dev/null; then
    echo "VIPM activation: FAIL: could not verify VIPM activation" >&2
    exit 1
fi
python3 - "$about_json" <<'PY'
import json
import pathlib
import sys

about = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
expected = {
    "version": "26.3.2",
    "edition": "Professional",
    "valid_through": "Perpetual",
    "valid_activation_code": True,
}
if any(about.get(name) != value for name, value in expected.items()):
    print("VIPM activation: FAIL: VIPM did not report a valid perpetual Professional activation", file=sys.stderr)
    raise SystemExit(1)
PY

unset VIPM_SERIAL_NUMBER VIPM_FULL_NAME VIPM_EMAIL
echo "VIPM activation: PASS: VIPM 26.3.2 Professional activation verified"