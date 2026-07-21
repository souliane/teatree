#!/usr/bin/env bash
# Inner-loop FAST CI parity (#122): scoped checks, NO coverage floor (partial data
# must never be floor-judged — the same rule the CI shards follow). Iterate here;
# run the full `bash dev/ci-parity.sh` ONCE before pushing (only it and CI's
# `test (3.13)` combiner can prove the 93% whole-tree floor).
set -euo pipefail
cd "$(dirname "$0")/.."

# Skip the slow network hooks (their own dedicated CI jobs run them).
export SKIP="${SKIP:-uv-audit,cyclonedx-sbom}"

echo "=== [1/3] prek on changed files (fast, not --all-files) ==="
# `uv run` so the prek runner is the lockfile-pinned prek CI runs (#3236).
uv run prek run

echo "=== [2/3] makemigrations --check --dry-run -- migration-graph linearity ==="
uv run python manage.py makemigrations --check --dry-run

echo "=== [3/3] affected tests + incremental push gate (scoped doctest + ast-grep) ==="
# The affected-tests selector runs only the tests a diff touches, degrading to the
# WHOLE suite on any unclassifiable change (over-run is free, under-run is a false
# green). CI's sharded `test (3.13)` lane stays the whole-tree authority either way.
bash dev/test-affected.sh
uv run t3 tool push-gate --run

echo "=== ci-parity-fast: scoped pre-push checks passed (NO coverage floor -- CI's required test lane owns it) ==="
