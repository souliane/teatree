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

echo "=== [3/3] scoped tests/quality + incremental push gate (scoped doctest + ast-grep) ==="
uv run pytest tests/quality -m "not push_heavy" -q
uv run t3 tool push-gate --run

echo "=== ci-parity-fast: scoped inner-loop checks passed (NO coverage floor -- run dev/ci-parity.sh before push) ==="
