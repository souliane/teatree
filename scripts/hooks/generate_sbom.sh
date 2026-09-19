#!/usr/bin/env bash
set -euo pipefail

tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT

mkdir -p dist
# ``--locked``: a pyproject dependency edit committed without a re-lock must fail
# with uv's own "lockfile out of date" message rather than have the hook rewrite
# uv.lock behind the commit. CI and the weekly lock-upgrade workflow both sync
# first, so both stay consistent.
uv export --locked --no-hashes --no-dev --no-emit-project -o "$tmp"
uv run cyclonedx-py requirements "$tmp" \
  --pyproject pyproject.toml \
  --of JSON \
  --output-reproducible \
  -o dist/sbom.json

# cyclonedx-py omits the trailing newline; end-of-file-fixer adds one.
# Append here so regeneration stays idempotent with the committed file.
printf '\n' >> dist/sbom.json
