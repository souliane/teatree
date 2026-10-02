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
# loses zero coverage.
#
# `tests/conformance` is on the push path only as its TOTALITY CORE, serial and
# capped. A conformance test's INPUT is the whole tree, so a diff-scoped selector
# cannot decide it is unaffected — a new scanner kind with no dispatch/statusline
# route breaks `test_signal_route_totality.py` whichever module the diff names, and
# that class reached CI twice (#3787, #3788). The whole directory is not affordable
# at push. Measured 2026-09-05 on a 10-core Mac (24 GiB, zero swap) at host load
# 19-35: all 60 files SERIAL cost 276s of pytest time, 350s wall, 1.72 GB peak RSS.
# At `-n auto` inside the 6.0 GiB worker container (4-CPU quota, host load 37-53)
# it ran 19+ minutes on three pushes of one tree (#172). The cost is structural:
# `test_scope_dependent_callers.py` parses the whole `tests/` tree (2743 files,
# +800 MiB per PROCESS, paid again by every xdist worker), `test_session_start_latency.py`
# asserts wall-clock budgets against real hook subprocesses, and one git-fixture
# setup alone takes 21s. Those lanes are CI-only — both CI lanes run `tests/`
# whole-tree on every MR. The core below is every registry/route totality lane whose
# input is `src/teatree` alone: no subprocess, no clock, no DB, no tests-tree parse
# (the composition the guard test pins). Measured the same day, same box, serial at
# host load 20-24: ~40s of pytest time, ~65s wall including collection.
set -euo pipefail
# PHYSICAL, because `dev/` is reachable through a symlink: a caller that invokes this
# through one gets `..` applied to the LINK's parent, which is a different tree, and
# every path below (`scripts/hooks/lib/resolve-uv.sh` first) then resolves nowhere.
cd "$(cd -P "$(dirname "$0")" && pwd)/.."

# GNU timeout(1) caps the conformance core below. Linux ships it as `timeout`;
# Homebrew's coreutils installs it as `gtimeout` unless the gnubin directory is
# put first on PATH (prerequisites.md) — probing only `timeout` blocked every
# macOS push with a message that named a fix which did not apply.
timeout_bin=""
for candidate in timeout gtimeout; do
    if command -v "$candidate" >/dev/null; then
        timeout_bin="$candidate"
        break
    fi
done
if [ -z "$timeout_bin" ]; then
    echo "push-gate: no GNU timeout(1) found (checked: timeout, gtimeout) — brew install coreutils on macOS (Linux ships it); see prerequisites.md" >&2
    exit 2
fi

# Record the attempt before waiting so a hard kill still leaves its last reached point.
. dev/lib/gate-record.sh
start_push_gate_record
trap 'finish_push_gate_record "$?"' EXIT

# Serialise the machine-wide memory-heavy sweep before measuring its worker budget.
. dev/lib/gate-lock.sh
gate_lock_rc=0
acquire_push_gate_lock || gate_lock_rc=$?
record_push_gate_lock
if [ "$gate_lock_rc" -ne 0 ]; then
    exit "$gate_lock_rc"
fi

# `-n auto` sizes the worker pool from CPU count, which a cgroup memory cap does not
# change — so a memory-capped container spawns host-many workers and dies as an opaque
# xdist crash. Bound the pool from available cgroup memory instead (an explicit
# PYTEST_XDIST_AUTO_NUM_WORKERS is a ceiling; an uncapped box is left alone).
. dev/lib/xdist-workers.sh
bound_xdist_workers_to_memory
record_push_gate_bound

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

# Serial on purpose: 7 tests, measured 10s wall at `-n 0` against 19s at `-n auto`
# (2026-09-05, host load 20-24) — the pool costs more than the file.
echo "=== [1/3] never-lockout safety contract ==="
record_push_gate_stage 1
"${UV_PROJECT_RUN[@]}" run pytest tests/test_gate_never_lockout_contract.py -n 0 -q 9>&-

conformance_cap_s=120
conformance_core=(
    tests/conformance/test_advisory_verdict_points.py
    tests/conformance/test_attempt_records_its_spend.py
    tests/conformance/test_capability_surface_walk.py
    tests/conformance/test_config_key_classification.py
    tests/conformance/test_config_key_readers.py
    tests/conformance/test_consumer_caller_walk.py
    tests/conformance/test_gate_evidence_declared.py
    tests/conformance/test_gate_registry_walk.py
    tests/conformance/test_overlay_default_noops.py
    tests/conformance/test_preset_is_the_only_loop_switch.py
    tests/conformance/test_registry_parity.py
    tests/conformance/test_registry_parity_roster.py
    tests/conformance/test_signal_ledger_producers.py
    tests/conformance/test_signal_route_totality.py
    tests/conformance/test_step_post_conditions.py
    tests/conformance/test_suggested_skill_names_resolve.py
    tests/conformance/test_t3_master_gate_consumers.py
)
echo "=== [2/3] conformance totality core: serial, ${conformance_cap_s}s cap (whole-tree input, not diff-scopable) ==="
record_push_gate_stage 2
conformance_rc=0
# `--kill-after` because a child that ignores SIGTERM would otherwise wedge this
# wrapper (and the push) forever instead of failing loud.
"$timeout_bin" --kill-after=10s "$conformance_cap_s" "${UV_PROJECT_RUN[@]}" run pytest "${conformance_core[@]}" -n 0 -q 9>&- || conformance_rc=$?
if [ "$conformance_rc" -eq 124 ]; then
    echo "=== push-gate: conformance core NOT verified here — it exceeded its ${conformance_cap_s}s cap ($(uptime)); CI runs tests/conformance whole-tree on this MR ===" >&2
elif [ "$conformance_rc" -ne 0 ]; then
    exit "$conformance_rc"
fi

echo "=== [3/3] incremental push gate: scoped doctest + ast-grep (FULL on uncertainty) ==="
record_push_gate_stage 3
"${UV_PROJECT_RUN[@]}" run t3 tool push-gate --run 9>&-
