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
# Matches that only appear inside email addresses are ignored so author/contact
# metadata can stay intact while still blocking leaked tenant/project terms.

set -euo pipefail

terms=""

# Parse --config argument
if [[ "${1:-}" == "--config" ]]; then
  config="${2:-}"
  shift 2
  if [ -n "$config" ]; then
    config="${config/#\~/$HOME}"
  fi
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
  matches=$(python3 - "$file" "$pattern" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
pattern = re.compile(rf"\b({sys.argv[2]})\b", re.IGNORECASE)
email_pattern = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
    email_spans = [match.span() for match in email_pattern.finditer(line)]
    has_non_email_match = False
    for match in pattern.finditer(line):
        if any(start <= match.start() and match.end() <= end for start, end in email_spans):
            continue
        has_non_email_match = True
        break
    if has_non_email_match:
        print(f"{line_number}:{line}")
PY
)
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
