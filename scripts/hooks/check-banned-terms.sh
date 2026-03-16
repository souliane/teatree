#!/usr/bin/env bash
# Pre-commit hook: reject files containing banned terms.
#
# Reads terms from a user-local config file:
#   --config <path>  Shell KEY=VALUE file. Reads *BANNED_TERMS= variable.
#
# Example .pre-commit-config.yaml entry:
#   entry: scripts/hooks/check-banned-terms.sh --config ~/.teatree
#
# The config file (e.g., ~/.teatree) should contain:
#   T3_BANNED_TERMS="term1,term2,term3"
#
# If no config or no BANNED_TERMS variable, exits 0 (no-op).

set -euo pipefail

terms=""

# Parse --config argument
if [[ "${1:-}" == "--config" ]]; then
  config="${2:-}"
  shift 2
  if [ -n "$config" ] && [ -f "$config" ]; then
    terms="$(grep -E '_?BANNED_TERMS=' "$config" 2>/dev/null | head -1 | sed 's/^.*BANNED_TERMS=//' | sed 's/^["'"'"']//;s/["'"'"']$//')"
  fi
fi

if [ -z "$terms" ]; then
  exit 0
fi

# Build a grep pattern from comma-separated terms
pattern=""
IFS=',' read -ra term_array <<< "$terms"
for term in "${term_array[@]}"; do
  term="$(echo "$term" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
  [ -z "$term" ] && continue
  if [ -n "$pattern" ]; then
    pattern="$pattern|$term"
  else
    pattern="$term"
  fi
done

if [ -z "$pattern" ]; then
  exit 0
fi

# Check each staged file passed by pre-commit
found=0
for file in "$@"; do
  [ -f "$file" ] || continue
  matches=$(grep -niE "\b($pattern)\b" "$file" 2>/dev/null || true)
  if [ -n "$matches" ]; then
    echo "BANNED TERM in $file:"
    echo "$matches" | sed 's/^/  /'
    found=1
  fi
done

if [ "$found" -ne 0 ]; then
  echo ""
  echo "Banned terms: $terms"
  echo "These terms must not appear in this repo."
  exit 1
fi
