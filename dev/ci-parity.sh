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

if [ "${LINT_DOCKER:-0}" = "1" ]; then
  echo "=== [1/6] prek (all hooks, all files) -- CI lint job, IN DOCKER (LINT_DOCKER=1) ==="
  # Exact CI-lint reproduction: build the same `lint` Dockerfile stage CI's
  # `build-image` job bakes (prek's hook environments pre-installed) and run
  # the identical `prek run --all-files` inside it, bind-mounting the working
  # tree the same way the CI `lint` job does. Builds locally rather than
  # pulling the ghcr-pushed tag, so this stays a zero-setup opt-in (no
  # registry auth needed) — a genuine environment-only lint difference (a
  # baked hook env vs whatever `uv run prek` resolves on the host) surfaces
  # here that the plain host-native invocation below can never catch.
  docker build -q -f dev/Dockerfile.test --target lint -t teatree-lint-local . >/dev/null
  docker run --rm -v "$PWD":/app -e SKIP -e T3_BANNED_TERMS -e TEATREE_TERM_REGISTRY teatree-lint-local \
    bash -c "uv run prek run --all-files"
else
  echo "=== [1/6] prek (all hooks, all files) -- CI lint job ==="
  # `uv run` so the prek RUNNER is the lockfile-pinned version (prek==0.4.10), the
  # exact one CI's lint job runs — not whatever standalone prek is on PATH (#3236).
  uv run prek run --all-files
fi

echo "=== [2/6] makemigrations --check --dry-run -- migration-graph linearity ==="
uv run python manage.py makemigrations --check --dry-run

echo "=== [3/6] test-path-mirror ratchet -- tests mirror src ==="
uv run t3 tool test-path-mirror --root .

echo "=== [4/6] module-health ratchet vs base -- CI module-health-gate job ==="
# CI's module-health-gate runs the LOC/OOP/typed-data ratchet in --from-ref diff
# mode over the PR's base..head range (.github/workflows/ci.yml). The prek
# `module-health` hook is commit-msg-stage, so step 1's `prek run --all-files`
# (default stage only) never fires it — a file crossing the LOC cap read green
# here until this step was added (souliane/teatree#3506). BASE_REF names the PR
# base the diff is taken against; default `main` for a local pre-push check.
git fetch --no-tags origin "${BASE_REF:-main}"
uv run python scripts/hooks/check_module_health.py --from-ref "origin/${BASE_REF:-main}"

echo "=== [5/6] coverage lane -- full suite, doctests, 93% branch floor ==="
bash dev/test-cov.sh

echo "=== [6/6] per-module coverage floors -- t3 ci coverage ==="
uv run t3 ci coverage

echo "=== ci-parity: all blocking CI predicates passed ==="
