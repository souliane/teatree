#!/usr/bin/env bash
# Pre-commit hook: detect new lint suppressions that weren't in the previous commit.
# Exits non-zero if new noqa, type: ignore, per-file-ignores, or coverage exclusions are added.
set -euo pipefail

STAGED_DIFF=$(git diff --cached --unified=0 -- '*.py' 'pyproject.toml')

# Count new suppression lines (lines starting with +, excluding the +++ header)
NEW_NOQA=$(echo "$STAGED_DIFF" | grep -c '^\+.*# noqa' || true)
NEW_TYPE_IGNORE=$(echo "$STAGED_DIFF" | grep -c '^\+.*# type: ignore' || true)
NEW_PER_FILE=$(echo "$STAGED_DIFF" | grep -c '^\+.*per-file-ignores' || true)
NEW_PRAGMA=$(echo "$STAGED_DIFF" | grep -c '^\+.*pragma: no cover' || true)
NEW_FAIL_UNDER=$(echo "$STAGED_DIFF" | grep -c '^\+.*fail_under' || true)

TOTAL=$((NEW_NOQA + NEW_TYPE_IGNORE + NEW_PER_FILE + NEW_PRAGMA + NEW_FAIL_UNDER))

if [ "$TOTAL" -gt 0 ]; then
    echo "Quality erosion detected in staged changes:"
    [ "$NEW_NOQA" -gt 0 ] && echo "  - $NEW_NOQA new # noqa comment(s)"
    [ "$NEW_TYPE_IGNORE" -gt 0 ] && echo "  - $NEW_TYPE_IGNORE new # type: ignore comment(s)"
    [ "$NEW_PER_FILE" -gt 0 ] && echo "  - $NEW_PER_FILE new per-file-ignores entry(ies)"
    [ "$NEW_PRAGMA" -gt 0 ] && echo "  - $NEW_PRAGMA new pragma: no cover comment(s)"
    [ "$NEW_FAIL_UNDER" -gt 0 ] && echo "  - $NEW_FAIL_UNDER fail_under change(s)"
    echo ""
    echo "Review each suppression. If justified, commit with SKIP=check-quality-erosion."
    exit 1
fi
