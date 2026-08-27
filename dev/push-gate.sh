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
# PHYSICAL, because `dev/` is reachable through a symlink: a caller that invokes this
# through one gets `..` applied to the LINK's parent, which is a different tree, and
# every path below (`scripts/hooks/lib/resolve-uv.sh` first) then resolves nowhere.
cd "$(cd -P "$(dirname "$0")" && pwd)/.."

# `-n auto` sizes the worker pool from CPU count, which a cgroup memory cap does not
# change — so a memory-capped container spawns host-many workers and dies as an opaque
# xdist crash. Default the pool from the cap instead (an explicit
# PYTEST_XDIST_AUTO_NUM_WORKERS still wins; an uncapped box is left alone).
. dev/lib/xdist-workers.sh
bound_xdist_workers_to_memory

# A bare `uv run` from a workspace MEMBER syncs the ROOT's shared `.venv`, reconciling it
# to the member's dependency set while this gate is importing from it — so the run dies
# on ImportErrors naming packages no diff went near. `uv_project_run_prefix` redirects to
# an environment the hooks own; `|| rc=$?` because `if !` reports the NEGATION.
. scripts/hooks/lib/resolve-uv.sh
uv_resolution_rc=0
uv_bin="$(resolve_uv)" || uv_resolution_rc=$?
if [ "${uv_resolution_rc}" -ne 0 ]; then
    echo "push-gate: no usable uv (resolve_uv rc=${uv_resolution_rc}) — see scripts/hooks/lib/resolve-uv.sh" >&2
    exit 2
fi
uv_project_run_prefix "${uv_bin}" "$PWD"

echo "=== [1/3] never-lockout safety contract ==="
"${UV_PROJECT_RUN[@]}" run pytest tests/test_gate_never_lockout_contract.py -q

echo "=== [2/3] conformance lane: registry/route totality (whole-tree input, not diff-scopable) ==="
"${UV_PROJECT_RUN[@]}" run pytest tests/conformance -q

echo "=== [3/3] incremental push gate: scoped doctest + ast-grep (FULL on uncertainty) ==="
"${UV_PROJECT_RUN[@]}" run t3 tool push-gate --run
