#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo "usage: install-grype.sh <version> <sha256> <install-directory>" >&2
  exit 2
fi

version=$1
expected_sha256=$2
install_directory=$3
if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "Grype version must be an exact semantic version." >&2
  exit 2
fi
if [[ ! "$expected_sha256" =~ ^[0-9a-f]{64}$ ]]; then
  echo "Grype SHA-256 must be 64 lowercase hexadecimal characters." >&2
  exit 2
fi

archive_name="grype_${version}_linux_amd64.tar.gz"
url="https://github.com/anchore/grype/releases/download/v${version}/${archive_name}"
temporary_directory=$(mktemp -d)
trap 'rm -rf "$temporary_directory"' EXIT
archive_path="$temporary_directory/$archive_name"
curl --fail --location --silent --show-error --retry 3 "$url" --output "$archive_path"
if ! printf '%s  %s\n' "$expected_sha256" "$archive_path" | sha256sum --check --status; then
  echo "Grype archive SHA-256 mismatch." >&2
  exit 1
fi

tar --extract --gzip --file "$archive_path" --directory "$temporary_directory" grype
if [[ ! -f "$temporary_directory/grype" || -L "$temporary_directory/grype" ]]; then
  echo "Verified Grype archive does not contain a regular grype executable." >&2
  exit 1
fi
mkdir -p "$install_directory"
install -m 0755 "$temporary_directory/grype" "$install_directory/grype"