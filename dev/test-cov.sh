#!/usr/bin/env bash
# Reproduce the CI coverage gate locally: the full suite, parallel, WITH
# coverage + doctests + the 93% floor. The default `uv run pytest` (and
# dev/test-fast.sh) run lean and parallel with NO coverage. Run this before
# pushing a change that could move the floor.
#
# This is the SINGLE-PROCESS parity lane. CI shards the same measurement 4 ways
# (`test-shard` matrix + `test` combiner) for wall-clock, but the floor is
# identical — combined shard coverage vs. this one-process run give the same
# percentage. Semantics here are pinned (tests/test_coverage_floor_guard.py and
# other PRs depend on the exact flags); do not change them.
set -euo pipefail
cd "$(dirname "$0")/.."

PY_VERSION="${TEATREE_TEST_PYTHON:-3.13}"

echo "=== Coverage gate: Python ${PY_VERSION} (host, parallel, 93% floor) ==="
exec uv run -p "${PY_VERSION}" pytest \
    --no-header -q -n auto \
    --doctest-modules --cov --cov-branch \
    --cov-report=term-missing:skip-covered \
    --cov-fail-under=93 \
    "$@"
