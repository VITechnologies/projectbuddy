#!/usr/bin/env bash
set -euo pipefail

version=${1:-}
remote=${2:-origin}

if [[ ! "$version" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "::error::Invalid release version; expected vX.Y.Z."
  exit 2
fi

lookup_stderr=$(mktemp)
trap 'rm -f "$lookup_stderr"' EXIT

set +e
lookup_output=$(git ls-remote --exit-code --tags "$remote" "refs/tags/$version" 2>"$lookup_stderr")
lookup_status=$?
set -e

if [[ $lookup_status -eq 2 && -z "$lookup_output" && ! -s "$lookup_stderr" ]]; then
  echo "::notice::Tag $version is absent from the remote."
  exit 0
fi

if [[ $lookup_status -eq 0 ]]; then
  if [[ -n "$lookup_output" && "$lookup_output" != *$'\n'* ]]; then
    oid=${lookup_output%%$'\t'*}
    expected_line=$oid$'\t'"refs/tags/$version"
    if [[ "$lookup_output" == "$expected_line" &&
          ( "$oid" =~ ^[0-9a-f]{40}$ || "$oid" =~ ^[0-9a-f]{64}$ ) ]]; then
      echo "::error::Tag $version already exists; choose a new version."
      exit 1
    fi
  fi

  echo "::error::Tag lookup returned malformed or ambiguous output; refusing release creation."
  exit 2
fi

echo "::error::Remote tag lookup failed (git ls-remote exit $lookup_status); refusing release creation."
exit 2
