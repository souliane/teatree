#!/usr/bin/env bash
# Pre-commit hook: reject files containing banned terms.
#
# Reads banned_terms from a TOML config file (e.g., ~/.teatree.toml):
#   --config <path>  TOML file with a banned_terms array in any section.
#
# Example .pre-commit-config.yaml entry:
#   entry: scripts/hooks/check-banned-terms.sh --config ~/.teatree.toml
#
# Example TOML:
#   [teatree]
#   banned_terms = ["term1", "term2"]
#
# If no config or no banned_terms key, exits 0 (no-op).
#
# This is a THIN wrapper: all matching is delegated to
# ``teatree.hooks.banned_terms_cli`` (which uses ``teatree.hooks.term_match``),
# so the shell hook, the in-process posting gate and the overlay-leak gate all
# run ONE matcher. The hook reimplemented the tokenizer in bash-inlined Python
# before; that second copy could drift silently from term_match. A parity
# meta-test (tests/test_banned_terms_parity.py) pins all entry points to the
# same verdict on a golden corpus so they cannot diverge again.

set -euo pipefail

# Resolve the repo root from this script's own location
# (scripts/hooks/check-banned-terms.sh -> repo root) so the CLI runs against
# THIS clone's teatree install regardless of the caller's cwd.
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"

# Prefer ``uv run`` so the matcher comes from this repo's environment (critical
# in a worktree, where a bare ``python3`` may import a different editable
# install). Fall back to ``python3 -m`` when uv is unavailable.
if command -v uv >/dev/null 2>&1; then
  exec uv run --project "${repo_root}" python -m teatree.hooks.banned_terms_cli "$@"
else
  exec env PYTHONPATH="${repo_root}/src${PYTHONPATH:+:${PYTHONPATH}}" \
    python3 -m teatree.hooks.banned_terms_cli "$@"
fi
