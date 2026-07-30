#!/usr/bin/env bash
# Reproduce ONE CI test shard's EXACT slice locally, before pushing (#3160).
#
# A shard-only red ("green locally, red in shard 3") happens because the CI
# `test-shard` matrix runs a duration-balanced TWELFTH of the suite in the tree's
# committed order — a slice/adjacency no ordinary local run reproduces. This runs
# the same pytest-split slice with the SAME flags the CI shard uses
# (`--splits 12 --group N --durations-path dev/.test_durations
# --splitting-algorithm least_duration --doctest-modules`), so the failing shard
# is reproducible on your box.
#
# The leak sentinel runs WARN by default (names any process-global env/cwd
# polluter without failing); set `LEAK_SENTINEL=error` to turn the polluter into
# a hard local failure. Append `-n0` for the deterministic serial order that most
# faithfully reproduces an order-dependent shard red (xdist honours the last -n).
#
# Usage:
#   dev/ci-shard.sh <group>                 # group of 12, e.g. dev/ci-shard.sh 3
#   dev/ci-shard.sh <group> --splits <N>    # a different split count
#   dev/ci-shard.sh 3 -n0                    # serial, deterministic order
#   LEAK_SENTINEL=error dev/ci-shard.sh 3    # fail the polluter locally
set -euo pipefail
cd "$(dirname "$0")/.."

GROUP="${1:?usage: dev/ci-shard.sh <group 1..N> [--splits N] [extra pytest args]}"
shift

SPLITS=12
if [ "${1:-}" = "--splits" ]; then
    SPLITS="${2:?--splits needs a value}"
    shift 2
fi

LEAK_MODE="${LEAK_SENTINEL:-warn}"

echo "=== CI shard ${GROUP}/${SPLITS} (least_duration split, leak-sentinel=${LEAK_MODE}) ==="
exec uv run --group shard pytest --no-header -q -n auto \
    --doctest-modules --cov --cov-branch --cov-report= --cov-fail-under=0 \
    --splits "${SPLITS}" --group "${GROUP}" \
    --durations-path dev/.test_durations \
    --splitting-algorithm least_duration \
    -p scripts.ci.leak_sentinel_plugin --leak-sentinel="${LEAK_MODE}" \
    "$@"
