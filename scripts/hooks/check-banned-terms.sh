#!/usr/bin/env bash
# Pre-commit hook: reject files containing banned terms.
#
# Usage:
#   check-banned-terms.sh --terms "term1,term2" [files...]
#   check-banned-terms.sh [files...]   # falls back to T3_BANNED_TERMS in ~/.teatree
#
# Exits 0 if clean, 1 if any banned term is found.

set -euo pipefail

# Parse --terms argument if provided
terms=""
if [[ "${1:-}" == "--terms" ]]; then
  terms="${2:-}"
  shift 2
fi

# Fallback: read from ~/.teatree
if [ -z "$terms" ]; then
  config="$HOME/.teatree"
  if [ -f "$config" ]; then
    terms="$(grep -E '^T3_BANNED_TERMS=' "$config" 2>/dev/null | head -1 | sed 's/^T3_BANNED_TERMS=//' | sed 's/^["'"'"']//;s/["'"'"']$//')"
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
