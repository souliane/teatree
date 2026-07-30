#!/usr/bin/env bash
# The push-stage CI-critical parity gate (#122); guarded by
# tests/test_no_full_suite_on_pre_push.py. Must never run the whole local suite
# (#112/#21/#38 — a loaded host times out unrelated wall-clock tests; push -> CI
# is the gate) and never the 93% coverage floor (a whole-tree property no diff
# subset can prove — that stays in `dev/ci-parity.sh`, the CI `test (3.13)` lane,
# and the untouched CI whole-tree backstop). This gate is a fast EARLY signal.
#
# The broad `tests/quality` directory is CI-only: even with `push_heavy` deselected
# its ~666 subprocess-spawning tests ran ~420s locally (`-n auto`), dwarfing this
# gate's whole point (a fast early signal) and hitting the push-hook wall-clock cap.
# CI's `test (3.13)` shard runs it whole-tree on every PR, so relocating it here
# loses zero coverage. What stays on the push path is the never-lockout safety
# contract (catch a self-lockout BEFORE it is pushed), the whole-tree CONFORMANCE
# lane, and the incremental push gate (scoped to the diff, FULL on any uncertainty).
#
# WHY `tests/conformance` is on the push path (unlike `tests/quality`): a conformance
# test's INPUT is the whole tree, so a diff-scoped selector cannot decide it is
# unaffected — a new scanner kind with no dispatch/statusline route breaks
# `test_signal_route_totality.py` no matter which module the diff names. That class
# reached CI twice (#3787, #3788) because the author's hand-picked local dir list
# (`pytest tests/teatree_core tests/teatree_loop ...`) excluded the dir, and the one
# lane that does force-keep it (`dev/test-affected.sh`, FLOOR_DIRS) is opt-in. Here it
# is unconditional. Measured 34s at `-n auto` — a tenth of the `tests/quality` dir it
# is explicitly NOT joining, and bounded (it never grows with the diff).
set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== [1/3] never-lockout safety contract ==="
uv run pytest tests/test_gate_never_lockout_contract.py -q

echo "=== [2/3] conformance lane: registry/route totality (whole-tree input, not diff-scopable) ==="
uv run pytest tests/conformance -q

echo "=== [3/3] incremental push gate: scoped doctest + ast-grep (FULL on uncertainty) ==="
uv run t3 tool push-gate --run
