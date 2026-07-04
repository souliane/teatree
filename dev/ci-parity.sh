#!/usr/bin/env bash
# The exact blocking CI predicate, in one command. Run this before pushing a
# src-touching PR (mandated by CLAUDE.md "Running things" and /t3:ship) so a
# genuine floor/gate failure is caught locally instead of on the first CI cycle.
#
# This is OPT-IN BY WORKFLOW, NEVER a push hook: the 93% whole-tree branch
# coverage floor (step 4/5) is a whole-tree property that no diff-scoped push
# subset can prove, and putting the full suite on the push path is exactly the
# friction tests/test_no_full_suite_on_pre_push.py forbids (#112/#21/#38). The
# push-stage `ci-critical-parity` hook covers the fast doctest/never-lockout
# classes at push time; THIS script is the complete predicate for a deliberate
# pre-push check.
set -euo pipefail
cd "$(dirname "$0")/.."

# Skip the slow network hooks (their own dedicated CI jobs run them); everything
# else runs exactly as CI's `lint` job does. Override with SKIP=... if needed.
export SKIP="${SKIP:-uv-audit,cyclonedx-sbom}"

echo "=== [1/5] prek (all hooks, all files) -- CI lint job ==="
prek run --all-files

echo "=== [2/5] makemigrations --check --dry-run -- migration-graph linearity ==="
uv run python manage.py makemigrations --check --dry-run

echo "=== [3/5] test-path-mirror ratchet -- tests mirror src ==="
uv run t3 tool test-path-mirror --root .

echo "=== [4/5] coverage lane -- full suite, doctests, 93% branch floor ==="
bash dev/test-cov.sh

echo "=== [5/5] per-module coverage floors -- t3 ci coverage ==="
uv run t3 ci coverage

echo "=== ci-parity: all blocking CI predicates passed ==="
