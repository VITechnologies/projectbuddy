#!/usr/bin/env bash
# ==========================================================================
# Run-Pipeline.sh
#
# Linux LabVIEW container pipeline. Runs LabVIEWCLI MassCompile and
# VI Analyzer headlessly inside the official NI LabVIEW Docker image
# (nationalinstruments/labview, see https://github.com/ni/labview-for-containers).
#
# Both modes fetch the example VIs if needed.
# Host mode (LabVIEWCLI not on PATH): pulls the image,
# mounts this repository at /workspace and re-runs this
# script inside the container.
# Container mode (LabVIEWCLI on PATH): runs the LabVIEWCLI operations.
#
# By default the target is NI's open-source example VIs
# (examples/cicd-examples/Test-VIs) pinned to LABVIEW_EXAMPLES_REF.
#
# Environment variables (all optional):
#   LABVIEW_IMAGE          Docker image (default: nationalinstruments/labview:2026q3-linux)
#   LV_YEAR                LabVIEW year inside the image (default: 2026)
#   LABVIEW_INSTALL_ROOT   NI LabVIEW installation root (default: /usr/local/natinst)
#   LABVIEW_PATH           LabVIEW executable inside the image
#                          (default: $LABVIEW_INSTALL_ROOT/LabVIEW-$LV_YEAR-64/labviewprofull;
#                          if that is absent, the script searches the install root)
#   LABVIEW_EXAMPLES_REPO  Example repository URL (default: https://github.com/ni/labview-for-containers.git)
#   LABVIEW_EXAMPLES_REF   Example commit SHA to check out
#   LABVIEW_TARGET_DIR     Repository-relative directory of VIs to process
#                          (default: .labview-examples/examples/cicd-examples/Test-VIs)
#   LABVIEW_VIA_CONFIG     Repository-relative VI Analyzer .viancfg file; set to an
#                          empty string to skip VI Analyzer
#                          (default: <target>/via-configs/via-config-pass.viancfg)
#   LABVIEW_OUTPUT_DIR     Repository-relative report directory (default: output/labview)
#   LABVIEW_PACKAGE_BUILD  Explicit unsupported flag. Setting this to a truthy value
#                          rejects package-build requests because this script is
#                          analysis-only and does not generate .vip build artifacts.
# ==========================================================================
set -euo pipefail

log() {
    printf '[%s] [%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$1" "$2"
}

fail() {
    log "ERROR" "$1"
    exit 1
}

is_truthy() {
    local value="${1:-}"
    [[ -n "$value" ]] && [[ "$value" =~ ^(1|true|TRUE|yes|YES|on|ON)$ ]]
}

if is_truthy "${LABVIEW_PACKAGE_BUILD:-}" || is_truthy "${BUILD_PACKAGES:-}" || is_truthy "${VIPM_BUILD:-}" || is_truthy "${PACKAGE_BUILD:-}"; then
    fail "Unsupported Linux package-build request: Run-Pipeline.sh is LabVIEW analysis-only and has no .vip package builds. Use Run-Pipeline.ps1 on self-hosted Windows for VIPM package generation, or use the Linux SBOM container workflow for manifest/SBOM-only execution."
fi

if [[ "$(uname -s)" != Linux ]]; then
    printf '%s\n' 'Run-Pipeline.sh supports Linux only. On Windows, use Run-Pipeline.ps1.' >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_NAME="$(basename "${BASH_SOURCE[0]}")"
CONTAINER_WORKSPACE="/workspace"
# Root used for the host-side output directory and the container's workspace
# mount -- distinct from SCRIPT_DIR, which stays this generic engine script's
# own location (where fetch_examples() fetches NI's example VIs by default).
# Defaults to SCRIPT_DIR so existing invocations are unaffected; a consumer
# invoking this script from a pinned external checkout (see
# docs/projectbuddy-consumption.md) must set this to the real repo root, so
# generated reports land where the calling workflow expects to upload them.
WORKSPACE_ROOT="${WORKSPACE_ROOT:-$SCRIPT_DIR}"

LABVIEW_IMAGE="${LABVIEW_IMAGE:-nationalinstruments/labview:2026q3-linux}"
LV_YEAR="${LV_YEAR:-2026}"
LABVIEW_INSTALL_ROOT="${LABVIEW_INSTALL_ROOT:-/usr/local/natinst}"
LABVIEW_EXAMPLES_REPO="${LABVIEW_EXAMPLES_REPO:-https://github.com/ni/labview-for-containers.git}"
LABVIEW_EXAMPLES_REF="${LABVIEW_EXAMPLES_REF:-2afb30b23bdb144368ee36d5f5ca33c06f800a98}"
EXAMPLES_DIR=".labview-examples"
EXAMPLES_SUBDIR="examples/cicd-examples/Test-VIs"
LABVIEW_TARGET_DIR="${LABVIEW_TARGET_DIR:-${EXAMPLES_DIR}/${EXAMPLES_SUBDIR}}"
LABVIEW_VIA_CONFIG="${LABVIEW_VIA_CONFIG-${LABVIEW_TARGET_DIR}/via-configs/via-config-pass.viancfg}"
LABVIEW_OUTPUT_DIR="${LABVIEW_OUTPUT_DIR:-output/labview}"

resolve_labview_path() {
    local preferred="${LABVIEW_PATH:-}"
    if [[ -n "$preferred" && -x "$preferred" ]]; then
        printf '%s\n' "$preferred"
        return 0
    fi

    local version_dir candidate name
    if [[ -d "${LABVIEW_INSTALL_ROOT}" ]] && command -v find >/dev/null 2>&1; then
        while IFS= read -r version_dir; do
            for name in labview labviewcommunity labviewprofull; do
                candidate="${version_dir}/${name}"
                if [[ -x "$candidate" ]]; then
                    printf '%s\n' "$candidate"
                    return 0
                fi
            done
        done < <(find "${LABVIEW_INSTALL_ROOT}" -mindepth 1 -maxdepth 1 -type d -name 'LabVIEW-*' 2>/dev/null | sort -V)
    fi

    local default_candidate="${LABVIEW_INSTALL_ROOT}/LabVIEW-${LV_YEAR}-64/labviewprofull"
    if [[ -x "$default_candidate" ]]; then
        printf '%s\n' "$default_candidate"
        return 0
    fi

    for name in labview labviewcommunity labviewprofull; do
        candidate="${LABVIEW_INSTALL_ROOT}/LabVIEW-${LV_YEAR}-64/${name}"
        if [[ -x "$candidate" ]]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done

    return 1
}

LABVIEW_PATH="${LABVIEW_PATH:-}"
if [[ -z "${LABVIEW_PATH}" ]]; then
    LABVIEW_PATH="${LABVIEW_INSTALL_ROOT}/LabVIEW-${LV_YEAR}-64/labviewprofull"
    if [[ ! -x "$LABVIEW_PATH" ]]; then
        LABVIEW_PATH="$(resolve_labview_path || true)"
    fi
fi

# Paths must stay inside the repository so they are visible at /workspace.
require_relative_path() {
    local name="$1" value="$2"
    if [[ "$value" == /* ]]; then
        fail "$name must be relative to the repository root: '$value'."
    fi
    if [[ "/$value/" == */../* ]]; then
        fail "$name must not contain '..' segments: '$value'."
    fi
}

require_relative_path "LABVIEW_TARGET_DIR" "$LABVIEW_TARGET_DIR"
require_relative_path "LABVIEW_OUTPUT_DIR" "$LABVIEW_OUTPUT_DIR"
if [[ -n "$LABVIEW_VIA_CONFIG" ]]; then
    require_relative_path "LABVIEW_VIA_CONFIG" "$LABVIEW_VIA_CONFIG"
fi

fetch_examples() {
    local dest="${SCRIPT_DIR}/${EXAMPLES_DIR}"
    if [[ -d "${dest}/${EXAMPLES_SUBDIR}" ]] && \
       [[ "$(git -C "$dest" rev-parse HEAD 2>/dev/null)" == "$LABVIEW_EXAMPLES_REF" ]]; then
        log "INFO" "NI example VIs already present at ${EXAMPLES_DIR} (${LABVIEW_EXAMPLES_REF})."
        return
    fi

    command -v git >/dev/null 2>&1 || fail "git is required to fetch the NI example VIs."
    log "INFO" "Fetching NI example VIs from ${LABVIEW_EXAMPLES_REPO} at ${LABVIEW_EXAMPLES_REF}..."
    rm -rf "$dest"
    git init --quiet "$dest"
    git -C "$dest" remote add origin "$LABVIEW_EXAMPLES_REPO"
    git -C "$dest" sparse-checkout set "$EXAMPLES_SUBDIR"
    git -C "$dest" fetch --quiet --depth 1 origin "$LABVIEW_EXAMPLES_REF"
    git -C "$dest" checkout --quiet FETCH_HEAD

    local head
    head="$(git -C "$dest" rev-parse HEAD)"
    [[ "$head" == "$LABVIEW_EXAMPLES_REF" ]] || \
        fail "Fetched example commit ${head} does not match ${LABVIEW_EXAMPLES_REF}."
}

run_on_host() {
    log "INFO" "LabVIEWCLI not found; running pipeline in ${LABVIEW_IMAGE}."
    command -v docker >/dev/null 2>&1 || fail "Docker is required to run the LabVIEW container."

    [[ -d "${SCRIPT_DIR}/${LABVIEW_TARGET_DIR}" ]] || \
        fail "Target directory '${LABVIEW_TARGET_DIR}' does not exist."

    mkdir -p "${WORKSPACE_ROOT}/${LABVIEW_OUTPUT_DIR}"

    log "INFO" "Pulling ${LABVIEW_IMAGE}..."
    docker pull "$LABVIEW_IMAGE"

    docker run --rm \
        -v "${WORKSPACE_ROOT}:${CONTAINER_WORKSPACE}" \
        -w "$CONTAINER_WORKSPACE" \
        -e LV_YEAR="$LV_YEAR" \
        -e LABVIEW_PATH="$LABVIEW_PATH" \
        -e LABVIEW_TARGET_DIR="$LABVIEW_TARGET_DIR" \
        -e LABVIEW_VIA_CONFIG="$LABVIEW_VIA_CONFIG" \
        -e LABVIEW_OUTPUT_DIR="$LABVIEW_OUTPUT_DIR" \
        -e HOST_UID="$(id -u)" \
        -e HOST_GID="$(id -g)" \
        "$LABVIEW_IMAGE" \
        bash "${CONTAINER_WORKSPACE}/${SCRIPT_NAME}"
}

# LabVIEWCLI leaves headless LabVIEW (and Xvfb) running with the caller's
# stdout inherited, so piping its output (e.g. to tee) never reaches EOF.
# Write to a log file instead and echo it afterwards.
run_cli() {
    local log_file="$1"
    shift
    local rc=0
    LabVIEWCLI "$@" -LabVIEWPath "$LABVIEW_PATH" -Headless >"$log_file" 2>&1 || rc=$?
    cat "$log_file"
    return "$rc"
}

finish_container_run() {
    LabVIEWCLI -OperationName CloseLabVIEW \
        -LabVIEWPath "$LABVIEW_PATH" -Headless >/dev/null 2>&1 </dev/null || true
    # The container runs as root; hand reports back to the invoking host user.
    if [[ -n "${HOST_UID:-}" && -n "${HOST_GID:-}" ]]; then
        chown -R "${HOST_UID}:${HOST_GID}" "${SCRIPT_DIR}/${LABVIEW_OUTPUT_DIR}" 2>/dev/null || true
    fi
}

run_in_container() {
    local target="${SCRIPT_DIR}/${LABVIEW_TARGET_DIR}"
    local output="${SCRIPT_DIR}/${LABVIEW_OUTPUT_DIR}"
    local status=0

    if [[ -z "${LABVIEW_PATH:-}" ]]; then
        LABVIEW_PATH="$(resolve_labview_path || true)"
    fi

    [[ -d "$target" ]] || fail "Target directory '${LABVIEW_TARGET_DIR}' does not exist."
    [[ -n "$LABVIEW_PATH" && -x "$LABVIEW_PATH" ]] || fail "LabVIEW executable not found at '${LABVIEW_PATH:-${LABVIEW_INSTALL_ROOT}/LabVIEW-${LV_YEAR}-64/labviewprofull}'. Set LV_YEAR, LABVIEW_INSTALL_ROOT, or LABVIEW_PATH."
    if [[ -n "$LABVIEW_VIA_CONFIG" && ! -f "${SCRIPT_DIR}/${LABVIEW_VIA_CONFIG}" ]]; then
        fail "VI Analyzer configuration '${LABVIEW_VIA_CONFIG}' does not exist."
    fi
    mkdir -p "$output"
    trap finish_container_run EXIT

    log "INFO" "=================================================="
    log "INFO" "LabVIEW Linux Container Pipeline"
    log "INFO" "LabVIEW: ${LABVIEW_PATH}"
    log "INFO" "Target:  ${LABVIEW_TARGET_DIR}"
    log "INFO" "Output:  ${LABVIEW_OUTPUT_DIR}"
    log "INFO" "=================================================="

    log "INFO" "Running MassCompile..."
    if run_cli "${output}/masscompile.log" -LogToConsole TRUE \
        -OperationName MassCompile \
        -DirectoryToCompile "$target"; then
        log "SUCCESS" "MassCompile completed."
    else
        log "ERROR" "MassCompile failed."
        status=1
    fi

    if [[ -z "$LABVIEW_VIA_CONFIG" ]]; then
        log "INFO" "LABVIEW_VIA_CONFIG is empty; skipping VI Analyzer."
    else
        local config="${SCRIPT_DIR}/${LABVIEW_VIA_CONFIG}"
        local report="${output}/vi-analyzer-results.txt"
        rm -f "$report"

        log "INFO" "Running VI Analyzer with ${LABVIEW_VIA_CONFIG}..."
        if ! run_cli "${output}/vi-analyzer.log" -LogToConsole TRUE \
            -OperationName RunVIAnalyzer \
            -ConfigPath "$config" \
            -ReportPath "$report"; then
            log "ERROR" "VI Analyzer operation failed."
            status=1
        fi

        if [[ -f "$report" ]]; then
            cat "$report"
            local failed
            failed="$(tr -d '\r' <"$report" | sed -n 's/^Failed Tests[[:space:]]*\([0-9]\+\)[[:space:]]*$/\1/p' | head -n 1)"
            if [[ -z "$failed" ]]; then
                log "ERROR" "Could not parse 'Failed Tests' from the VI Analyzer report."
                status=1
            elif (( failed > 0 )); then
                log "ERROR" "VI Analyzer reported ${failed} failed test(s)."
                status=1
            else
                log "SUCCESS" "VI Analyzer reported no failed tests."
            fi
        else
            log "ERROR" "VI Analyzer report was not generated."
            status=1
        fi
    fi

    if (( status == 0 )); then
        log "SUCCESS" "LabVIEW container pipeline completed."
    else
        log "ERROR" "LabVIEW container pipeline failed."
    fi
    return "$status"
}

if [[ "$LABVIEW_TARGET_DIR" == "${EXAMPLES_DIR}/"* ]]; then
    fetch_examples
fi

if command -v LabVIEWCLI >/dev/null 2>&1; then
    run_in_container
else
    run_on_host
fi
