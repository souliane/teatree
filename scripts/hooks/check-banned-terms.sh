#!/usr/bin/env bash
# Pre-commit hook: reject files containing banned terms.
#
# Reads banned_terms from a TOML config file (e.g., ~/.teatree.toml):
#   --config <path>  TOML file with a *banned_terms array in any section.
#
# Example .pre-commit-config.yaml entry:
#   entry: scripts/hooks/check-banned-terms.sh --config ~/.teatree.toml
#
# Example TOML:
#   [teatree]
#   banned_terms = ["term1", "term2"]
#
# If no config or no banned_terms key, exits 0 (no-op).
# Matching mirrors teatree.hooks.term_match (whole-token, camelCase-split, email carve-out).

set -euo pipefail

config=""

# Parse --config argument
if [[ "${1:-}" == "--config" ]]; then
  config="${2:-}"
  shift 2
  if [ -n "$config" ]; then
    config="${config/#\~/$HOME}"
  fi
fi

if [ -z "$config" ] || [ ! -f "$config" ]; then
  exit 0
fi

# Extract banned_terms from TOML using tomllib (stdlib since Python 3.11)
terms="$(python3 -c "
import tomllib, pathlib, sys
data = tomllib.loads(pathlib.Path(sys.argv[1]).read_text())
for v in list(data.values()) + [data]:
    if isinstance(v, dict) and 'banned_terms' in v:
        print(','.join(v['banned_terms']))
        break
" "$config" 2>/dev/null || true)"

if [ -z "$terms" ]; then
  exit 0
fi

# Check each staged file with the embedded whole-token matcher (email carve-out kept).
found=0
for file in "$@"; do
  [ -f "$file" ] || continue
  matches=$(python3 - "$file" "$terms" <<'PY'
import re
import sys
from pathlib import Path

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_CAMEL_BOUNDARY_RE = re.compile(r"([a-z0-9])([A-Z])")
_ACRONYM_BOUNDARY_RE = re.compile(r"([A-Z]+)([A-Z][a-z])")


def _tokens(text):
    split = _ACRONYM_BOUNDARY_RE.sub(r"\1 \2", text)
    split = _CAMEL_BOUNDARY_RE.sub(r"\1 \2", split)
    return _TOKEN_RE.findall(split.lower())


def _contains_run(haystack, needle):
    if not needle:
        return False
    if len(needle) == 1:
        return needle[0] in haystack
    span = len(needle)
    for start in range(len(haystack) - span + 1):
        if haystack[start : start + span] == needle:
            return True
    return "".join(needle) in haystack


path = Path(sys.argv[1])
terms = [t.strip() for t in sys.argv[2].split(",") if t.strip()]
term_tokens = [tt for tt in (_tokens(t) for t in terms) if tt]
email_pattern = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

if term_tokens:
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        # Strip emails first so a term only inside an author/contact email is not flagged.
        stripped = email_pattern.sub(" ", line)
        line_tokens = _tokens(stripped)
        if any(_contains_run(line_tokens, tt) for tt in term_tokens):
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
