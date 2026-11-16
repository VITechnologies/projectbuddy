#!/usr/bin/env bash
set -euo pipefail

# install-actions-runner.sh: registers this Ubuntu-LabVIEW VM as a
# self-hosted GitHub Actions runner over SSH (no interactive VRDE session
# required), the Linux/systemd analog of the Windows family's
# provision-windows-guest.py `register-runner` subcommand +
# Register-WindowsRunner.ps1. Deliberately does NOT touch LabVIEW/VIPM
# activation -- a registered, online runner with an unlicensed
# LabVIEW/VIPM install is a safe, inert state (same principle as the
# Windows family's own header comment for this phase).
#
# Runs as root (invoked by complete-runner-registration.sh via
# `sudo -S -p ""`): needed for dedicated-account creation and the
# actions-runner systemd service install. Registration itself
# (config.sh) deliberately drops to the unprivileged dedicated service
# account -- the actions/runner software refuses to run as root without
# the discouraged RUNNER_ALLOW_RUNASROOT escape hatch, matching the
# Windows family's own non-privileged-service-account requirement.
#
# Per REQ-020's established discipline (a past incident where a Windows
# register-runner self-reported success while the runner had actually
# registered under the wrong name): this script does NOT trust config.sh's
# own exit code alone as proof of a working runner -- it polls the real
# GitHub API afterward for an online runner with the expected name AND
# labels before reporting PASS.

usage() {
    cat <<'EOF'
usage: install-actions-runner.sh --repo <owner/repo> --runner-name <name> \
    --labels <comma,separated,labels> --service-account <name> \
    --runner-url <url> --runner-filename <file> --runner-sha256 <sha256> \
    --pat-file <mode-600 file containing a runner-registration PAT>
EOF
}

REPO=
RUNNER_NAME=
LABELS=
SERVICE_ACCOUNT=
RUNNER_URL=
RUNNER_FILENAME=
RUNNER_SHA256=
PAT_FILE=
while (($#)); do
    case "$1" in
        --repo) REPO="${2:-}"; shift 2 ;;
        --runner-name) RUNNER_NAME="${2:-}"; shift 2 ;;
        --labels) LABELS="${2:-}"; shift 2 ;;
        --service-account) SERVICE_ACCOUNT="${2:-}"; shift 2 ;;
        --runner-url) RUNNER_URL="${2:-}"; shift 2 ;;
        --runner-filename) RUNNER_FILENAME="${2:-}"; shift 2 ;;
        --runner-sha256) RUNNER_SHA256="${2:-}"; shift 2 ;;
        --pat-file) PAT_FILE="${2:-}"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "install-actions-runner: FAIL: unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done
for name in REPO RUNNER_NAME LABELS SERVICE_ACCOUNT RUNNER_URL RUNNER_FILENAME RUNNER_SHA256 PAT_FILE; do
    [[ -n "${!name}" ]] || { echo "install-actions-runner: FAIL: --${name,,} is required" >&2; usage >&2; exit 2; }
done
[[ "$REPO" =~ ^[^/[:space:]]+/[^/[:space:]]+$ ]] || {
    echo "install-actions-runner: FAIL: --repo must look like owner/repo" >&2
    exit 2
}
[[ "$SERVICE_ACCOUNT" =~ ^[a-z_][a-z0-9_-]*$ ]] || {
    echo "install-actions-runner: FAIL: --service-account is invalid" >&2
    exit 2
}
[[ "$(id -u)" == 0 ]] || { echo "install-actions-runner: FAIL: must run as root" >&2; exit 2; }
[[ -f "$PAT_FILE" && "$(stat -c '%a' "$PAT_FILE")" == 600 ]] || {
    echo "install-actions-runner: FAIL: --pat-file must exist with mode 600" >&2
    exit 2
}

if ! command -v gh >/dev/null 2>&1; then
    echo "install-actions-runner: installing gh CLI"
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /usr/share/keyrings/githubcli-archive-keyring.gpg
    chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        >/etc/apt/sources.list.d/github-cli.list
    apt-get update -y -qq
    apt-get install -y -qq gh
fi
command -v gh >/dev/null 2>&1 || { echo "install-actions-runner: FAIL: gh CLI not found on PATH after installation attempt" >&2; exit 1; }

if ! id -u "$SERVICE_ACCOUNT" >/dev/null 2>&1; then
    echo "install-actions-runner: creating dedicated service account $SERVICE_ACCOUNT"
    useradd --create-home --shell /bin/bash "$SERVICE_ACCOUNT"
fi
runner_root="/home/$SERVICE_ACCOUNT/actions-runner"
install -d -m 755 -o "$SERVICE_ACCOUNT" -g "$SERVICE_ACCOUNT" "$runner_root"

GH_TOKEN=
trap '
    unset GH_TOKEN
    [[ -n "${who:-}" ]] && gh auth logout --hostname github.com --user "$who" >/dev/null 2>&1 || true
    shred -u "$PAT_FILE" 2>/dev/null || rm -f "$PAT_FILE"
' EXIT
GH_TOKEN="$(cat "$PAT_FILE")"
export GH_TOKEN
who="$(gh api user --jq '.login')"
echo "install-actions-runner: gh authenticated as $who"

if [[ ! -x "$runner_root/config.sh" ]]; then
    echo "install-actions-runner: downloading actions-runner ($RUNNER_FILENAME)"
    tarball="$(mktemp)"
    curl -fsSL -o "$tarball" "$RUNNER_URL"
    actual_sha256="$(sha256sum "$tarball" | awk '{print $1}')"
    [[ "$actual_sha256" == "$RUNNER_SHA256" ]] || {
        echo "install-actions-runner: FAIL: actions-runner tarball SHA256 mismatch: expected $RUNNER_SHA256, got $actual_sha256" >&2
        rm -f "$tarball"
        exit 1
    }
    tar -xzf "$tarball" -C "$runner_root"
    rm -f "$tarball"
    chown -R "$SERVICE_ACCOUNT:$SERVICE_ACCOUNT" "$runner_root"
fi
echo "install-actions-runner: actions-runner present at $runner_root"

registration_token="$(gh api --method POST "repos/$REPO/actions/runners/registration-token" --jq '.token')"
[[ -n "$registration_token" ]] || { echo "install-actions-runner: FAIL: GitHub did not return a runner registration token" >&2; exit 1; }

if [[ -f "$runner_root/.runner" ]]; then
    # A prior run's systemd service may still be installed/running --
    # config.sh remove refuses to run ("Uninstall service first") until
    # it is stopped and uninstalled first.
    if [[ -f "$runner_root/svc.sh" ]] && systemctl list-units --all --no-legend 2>/dev/null | grep -q "actions\.runner\..*\.service"; then
        (cd "$runner_root" && ./svc.sh stop || true; ./svc.sh uninstall || true)
    fi
    sudo -u "$SERVICE_ACCOUNT" "$runner_root/config.sh" remove --token "$registration_token" || true
    registration_token="$(gh api --method POST "repos/$REPO/actions/runners/registration-token" --jq '.token')"
fi
sudo -u "$SERVICE_ACCOUNT" env -C "$runner_root" ./config.sh \
    --url "https://github.com/$REPO" \
    --token "$registration_token" \
    --name "$RUNNER_NAME" \
    --labels "$LABELS" \
    --work _work \
    --unattended \
    --replace
unset registration_token

if [[ -f "$runner_root/svc.sh" ]]; then
    (cd "$runner_root" && ./svc.sh install "$SERVICE_ACCOUNT" && ./svc.sh start)
fi

service_name="$(systemctl list-units --all --no-legend 2>/dev/null | awk '/actions\.runner\..*\.service/ {print $1; exit}')"
[[ -n "$service_name" ]] || { echo "install-actions-runner: FAIL: actions-runner systemd service not found after install" >&2; exit 1; }
for _ in $(seq 1 30); do
    state="$(systemctl is-active "$service_name" 2>/dev/null || true)"
    [[ "$state" == active ]] && break
    sleep 2
done
[[ "$state" == active ]] || { echo "install-actions-runner: FAIL: $service_name did not become active" >&2; exit 1; }
echo "install-actions-runner: systemd service $service_name is active"

expected_labels="self-hosted,Linux,X64,$LABELS"
deadline=$(( $(date +%s) + 90 ))
runners_json=
found_status=
while (( $(date +%s) < deadline )); do
    runners_json="$(gh api "repos/$REPO/actions/runners" --jq '.runners' || true)"
    if [[ -n "$runners_json" ]]; then
        found_status="$(python3 -c "
import json, sys
runners = json.loads(sys.argv[1])
name = sys.argv[2]
for r in runners:
    if r.get('name') == name:
        print(r.get('status', ''))
        break
" "$runners_json" "$RUNNER_NAME")"
        [[ "$found_status" == online ]] && break
    fi
    sleep 3
done
[[ "$found_status" == online ]] || { echo "install-actions-runner: FAIL: runner did not appear online in repos/$REPO/actions/runners within 90s" >&2; exit 1; }

python3 - "$runners_json" "$expected_labels" "$REPO" "$RUNNER_NAME" <<'PY'
import json
import sys

runners = json.loads(sys.argv[1])
expected_labels = set(sys.argv[2].split(","))
repo = sys.argv[3]
runner_name = sys.argv[4]
runner = next((r for r in runners if r.get("name") == runner_name), None)
if runner is None:
    print(f"install-actions-runner: FAIL: runner {runner_name!r} not found in the runners list", file=sys.stderr)
    raise SystemExit(1)
if runner.get("status") != "online":
    print(f"install-actions-runner: FAIL: runner status is {runner.get('status')!r}, not 'online'", file=sys.stderr)
    raise SystemExit(1)
actual_labels = {label["name"] for label in runner.get("labels", [])}
missing = expected_labels - actual_labels
if missing:
    print(f"install-actions-runner: FAIL: registered runner is missing expected labels: {sorted(missing)}", file=sys.stderr)
    raise SystemExit(1)
print(json.dumps({
    "schema": "projectbuddy/runner-registration-result@1",
    "repo": repo,
    "runnerId": runner.get("id"),
    "runnerName": runner.get("name"),
    "status": runner.get("status"),
    "busy": runner.get("busy"),
    "labels": sorted(actual_labels),
}, indent=2))
PY
echo "install-actions-runner: PASS: $RUNNER_NAME is online with expected labels"
