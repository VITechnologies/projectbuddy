#!/usr/bin/env bash
# Prepares the headless display VIPM needs inside NI's LabVIEW container,
# activates VIPM Pro from environment variables, then runs the command.
set -euo pipefail

if ! pgrep -x Xvfb >/dev/null; then
    Xvfb "${DISPLAY:-:99}" -screen 0 1280x720x24 -ac +extension GLX +render -noreset \
        >/tmp/xvfb.log 2>&1 &
fi
# The LabVIEW Runtime Engine used by VIPM requires this container marker.
mkdir -p /tmp/natinst && echo "1" >/tmp/natinst/LVContainer.txt

if [[ "${SBOM_SOURCE:-vipm}" != "manifest" ]]; then
    for name in VIPM_SERIAL_NUMBER VIPM_FULL_NAME VIPM_EMAIL; do
        if [[ -z "${!name:-}" ]]; then
            echo "[ERROR] ${name} is not set. VIPM Pro activation is required for 'vipm sbom'." >&2
            exit 1
        fi
    done
    echo "[INFO] Activating VIPM Pro..."
    # --direct-vipm-io activates without VIPM Desktop, whose first-run edition
    # dialog otherwise blocks activation on the headless display.
    vipm activate --direct-vipm-io --serial-number "$VIPM_SERIAL_NUMBER" --name "$VIPM_FULL_NAME" --email "$VIPM_EMAIL" >/tmp/vipm-activate.log 2>&1 || {
        sed "s/${VIPM_SERIAL_NUMBER}/***/g" /tmp/vipm-activate.log >&2
        echo "[ERROR] VIPM Pro activation failed." >&2
        exit 1
    }
fi
unset VIPM_SERIAL_NUMBER VIPM_FULL_NAME VIPM_EMAIL

exec "$@"
