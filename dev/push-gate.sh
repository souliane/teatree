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
# contract (catch a self-lockout BEFORE it is pushed) plus the incremental push
# gate (scoped to the diff, FULL on any uncertainty).
set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== [1/2] never-lockout safety contract ==="
uv run pytest tests/test_gate_never_lockout_contract.py -q

echo "=== [2/2] incremental push gate: scoped doctest + ast-grep (FULL on uncertainty) ==="
uv run t3 tool push-gate --run
