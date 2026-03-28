#!/usr/bin/env bash
# Pre-commit hook: detect additions of quality relaxation patterns.
# Exits non-zero if any relaxation is introduced without explicit approval.

set -euo pipefail

# Patterns that relax quality gates
RELAXATION_PATTERNS=(
    "# noqa"
    "# type: ignore"
    "SKIP="
    "--no-cov"
    "fail_under"
    "per-file-ignores"
    "pragma: no cover"
)

found=0
for pattern in "${RELAXATION_PATTERNS[@]}"; do
    matches=$(git diff --cached --diff-filter=A -U0 -- '*.py' '*.toml' '*.cfg' | grep -c "^\+.*${pattern}" 2>/dev/null || true)
    if [ "$matches" -gt 0 ]; then
        echo "WARNING: Quality relaxation detected: '$pattern' added ($matches occurrence(s))"
        found=1
    fi
done

if [ "$found" -eq 1 ]; then
    echo ""
    echo "Quality relaxation detected. If intentional, add a comment explaining why"
    echo "and re-commit. To bypass: SKIP=check-quality-relaxation git commit ..."
    exit 1
fi
