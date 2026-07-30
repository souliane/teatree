#!/usr/bin/env bash
# Local twin of CI's `test-shuffle` lane (souliane/teatree#2359 Class B): run the
# curated order-safe set under a SHUFFLED collection order, so a test that leaks
# process-global state and the victim it breaks land in a failing relative order.
#
# WHY THIS SCRIPT EXISTS AT ALL — a missing plugin group must never read as success.
# `pytest-randomly` is deliberately OUT of the default `dev` group (installing it there
# would shuffle every `uv run pytest` and the heavy coverage lane). So a hand-rolled
# `uv run pytest -n0 -q -p randomly ... | tail -5` against a plain `dev` env dies with
# `ImportError: Error importing plugin "randomly"` — and the PIPELINE still reports
# exit 0, because a shell pipeline's status is its LAST stage's. Measured on this repo:
# the bare pytest exits 1, the identical run piped into `tail` exits 0. Anything gating
# on that exit code reads a green shuffle lane that never collected a single test.
# Three guards close it, cheapest first:
#   1. `set -euo pipefail` and NO pipes — no exit code can be swallowed in here.
#   2. an explicit `--group shuffle` install plus an import PREFLIGHT that fails loud
#      with the fix command, BEFORE pytest is ever asked to load the plugin.
#   3. `-o required_plugins=pytest-randomly` — pytest's own in-session assertion, so a
#      degraded environment cannot silently produce an UNSHUFFLED green either.
# The curated directory set is pinned byte-for-byte against CI's lane by
# tests/test_ci_shuffle_lane_scope.py, so the local twin cannot drift from the gate.
#
# Usage:
#   bash dev/test-shuffle.sh          # seed 7 (the recorded #2359 reproducer)
#   bash dev/test-shuffle.sh 7 1 13 100   # CI's full matrix, serially
set -euo pipefail
cd "$(dirname "$0")/.."

SEEDS=("${@:-7}")

# pytest-randomly auto-activates on import, so leaving it installed would shuffle every
# later `uv run pytest` in this checkout — the exact destabilisation that keeps it out
# of the `dev` group. Restore the default environment on the way out; an EXIT trap that
# does not itself call `exit` leaves the script's own status untouched.
trap 'uv sync --quiet || true' EXIT

echo "=== [1/3] install the shuffle group (pytest-randomly is NOT in the default dev group) ==="
uv sync --group shuffle

echo "=== [2/3] preflight: pytest-randomly must be importable before we trust any result ==="
if ! uv run --group shuffle python -c 'import pytest_randomly'; then
    echo "FATAL: pytest-randomly is absent from this environment." >&2
    echo "       Without it, 'pytest -p randomly' exits non-zero and any pipe erases that" >&2
    echo "       exit code — a lane that ran NOTHING would report success. Fix and re-run:" >&2
    echo "         uv sync --group shuffle" >&2
    exit 1
fi

for seed in "${SEEDS[@]}"; do
    echo "=== [3/3] curated order-safe set under shuffled collection (seed ${seed}) ==="
    # -n0 is load-bearing: xdist would isolate a polluter from its victim across
    # workers, defeating the whole order-dependence audit.
    uv run --group shuffle pytest -n0 -q -p randomly \
        --randomly-seed="${seed}" \
        -o required_plugins=pytest-randomly \
        -p scripts.ci.leak_sentinel_plugin --leak-sentinel=warn \
        tests/teatree_loop/ \
        tests/config/ \
        tests/teatree_config/ \
        tests/teatree_utils/ \
        tests/utils/ \
        tests/teatree_quality/ \
        tests/messaging/ \
        tests/cli_doctor/ \
        tests/conformance/ \
        tests/teatree_hooks/
done

echo "=== test-shuffle: every seed passed under shuffled collection ==="
