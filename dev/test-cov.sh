#!/usr/bin/env bash
# Reproduce the CI coverage gate locally: the full suite, parallel, WITH
# coverage + doctests + the 93% floor. The default `uv run pytest` (and
# dev/test-fast.sh) run lean and parallel with NO coverage. Run this before
# pushing a change that could move the floor.
#
# This is the SINGLE-PROCESS parity lane. CI shards the same measurement 12 ways
# (`test-shard` matrix + `test` combiner) for wall-clock, but the floor is
# identical — combined shard coverage vs. this one-process run give the same
# percentage. Semantics here are pinned (tests/test_coverage_floor_guard.py and
# other PRs depend on the exact flags); do not change them.
set -euo pipefail
# PHYSICAL, because `dev/` is reachable through a symlink: a caller that invokes this
# through one gets `..` applied to the LINK's parent, which is a different tree, and
# every path below then resolves nowhere.
cd "$(cd -P "$(dirname "$0")" && pwd)/.."

# `-n auto` sizes the worker pool from CPU count, which a cgroup memory cap does not
# change — so a memory-capped container spawns host-many workers and dies as an opaque
# xdist crash. Default the pool from the cap instead (an explicit
# PYTEST_XDIST_AUTO_NUM_WORKERS still wins; an uncapped box is left alone).
. dev/lib/xdist-workers.sh
bound_xdist_workers_to_memory

PY_VERSION="${TEATREE_TEST_PYTHON:-3.13}"

echo "=== Coverage gate: Python ${PY_VERSION} (host, parallel, 93% floor) ==="
exec uv run -p "${PY_VERSION}" pytest \
    --no-header -q -n auto \
    --doctest-modules --cov --cov-branch \
    --cov-report=term-missing:skip-covered \
    --cov-fail-under=93 \
    "$@"
