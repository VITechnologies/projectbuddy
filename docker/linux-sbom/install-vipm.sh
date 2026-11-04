#!/usr/bin/env bash
set -euo pipefail

url=${1:?VIPM package URL is required}
expected_sha256=${2:?VIPM SHA-256 is required}
archive_path=${3:-/tmp/vipm.deb}

if [[ "$url" != https://* ]]; then
    printf 'VIPM package URL must use HTTPS.\n' >&2
    exit 2
fi
if [[ ! "$expected_sha256" =~ ^[[:xdigit:]]{64}$ ]]; then
    printf 'VIPM SHA-256 must contain 64 hexadecimal characters.\n' >&2
    exit 2
fi

curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' --output "$archive_path" "$url"
if ! printf '%s  %s\n' "$expected_sha256" "$archive_path" | sha256sum --check --status; then
    printf 'VIPM package SHA-256 mismatch.\n' >&2
    exit 1
fi

dpkg -i "$archive_path"
rm -f -- "$archive_path"
vipm --version
