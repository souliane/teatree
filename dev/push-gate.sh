#!/usr/bin/env bash
# The push-stage CI-critical parity gate (#122); guarded by
# tests/test_no_full_suite_on_pre_push.py. Must never run the whole local suite
# (#112/#21/#38 — a loaded host times out unrelated wall-clock tests; push -> CI
# is the gate) and never the 93% coverage floor (a whole-tree property no diff
# subset can prove — that stays in `dev/ci-parity.sh`, the CI `test (3.13)` lane,
# and the untouched CI whole-tree backstop). This gate is a fast EARLY signal.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== [1/2] scoped quality + never-lockout classes (push_heavy deselected) ==="
uv run pytest tests/quality tests/test_gate_never_lockout_contract.py -m "not push_heavy" -q

echo "=== [2/2] incremental push gate: scoped doctest + ast-grep (FULL on uncertainty) ==="
uv run t3 tool push-gate --run
