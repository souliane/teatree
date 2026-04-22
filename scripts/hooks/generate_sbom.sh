#!/usr/bin/env bash
set -euo pipefail

tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT

uv export --no-hashes --no-dev --no-emit-project -o "$tmp"
uv run cyclonedx-py requirements "$tmp" \
  --pyproject pyproject.toml \
  --of JSON \
  --output-reproducible \
  -o sbom.json

# cyclonedx-py omits the trailing newline; end-of-file-fixer adds one.
# Append here so regeneration stays idempotent with the committed file.
printf '\n' >> sbom.json
