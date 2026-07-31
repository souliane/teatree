#!/usr/bin/env bash
# Bound pytest-xdist's `-n auto` by the container's MEMORY cap, not just its cores.
#
# `-n auto` sizes the worker pool from the CPU count, and a cgroup memory limit does
# not change `nproc` — so inside a memory-capped container `-n auto` still sees the
# HOST's cores and spawns far more workers than the cap allows. The run then dies as
# an opaque xdist "worker crashed", which reads as a flaky lane rather than as the
# memory limit it actually is.
#
# Sourced (not executed) by every lane that runs pytest:
#
#   . "$(dirname "$0")/lib/xdist-workers.sh"
#   bound_xdist_workers_to_memory
#
# An explicit `PYTEST_XDIST_AUTO_NUM_WORKERS=4 bash dev/<lane>.sh` always wins — the
# bound only supplies a DEFAULT, and only when the cgroup reports a real cap that is
# tighter than the core count. An uncapped box is left entirely alone.

# Approximate resident memory one pytest-xdist worker needs for this suite. Override
# to re-tune the bound without touching the lanes.
: "${T3_MB_PER_TEST_WORKER:=512}"

# Injectable for tests; the defaults are the real cgroup v2 / v1 paths.
: "${T3_CGROUP_MEMORY_MAX_V2:=/sys/fs/cgroup/memory.max}"
: "${T3_CGROUP_MEMORY_MAX_V1:=/sys/fs/cgroup/memory/memory.limit_in_bytes}"

# Core count, injectable for the same reason. Empty means "detect with nproc"; a value
# pins it, so a test can exercise the memory arithmetic without its result depending on
# how many cores the runner happens to have.
: "${T3_CPU_COUNT:=}"

# cgroup v1 reports a near-2**63 page-aligned sentinel to mean "unlimited"; cgroup v2
# uses the literal string "max". Both mean "no cap" and must not bound anything.
_T3_CGROUP_UNLIMITED_SENTINEL=9223372036854771712

# Echo the cgroup memory cap in bytes, or return non-zero when uncapped/unreadable.
_t3_cgroup_memory_cap_bytes() {
    local raw=""
    if [ -r "$T3_CGROUP_MEMORY_MAX_V2" ]; then
        raw=$(cat "$T3_CGROUP_MEMORY_MAX_V2" 2>/dev/null || true)
    fi
    if [ -z "$raw" ] || [ "$raw" = "max" ]; then
        raw=""
        if [ -r "$T3_CGROUP_MEMORY_MAX_V1" ]; then
            raw=$(cat "$T3_CGROUP_MEMORY_MAX_V1" 2>/dev/null || true)
        fi
    fi
    case "$raw" in
        '' | max | *[!0-9]*) return 1 ;;
    esac
    if [ "$raw" -ge "$_T3_CGROUP_UNLIMITED_SENTINEL" ]; then
        return 1
    fi
    echo "$raw"
}

# Default PYTEST_XDIST_AUTO_NUM_WORKERS from the cgroup cap. Always returns 0 — a box
# with no cgroup, an unreadable cap, or an uncapped limit is simply left as it is.
bound_xdist_workers_to_memory() {
    if [ -n "${PYTEST_XDIST_AUTO_NUM_WORKERS:-}" ]; then
        return 0
    fi

    local cap
    cap=$(_t3_cgroup_memory_cap_bytes) || return 0

    local allowed=$((cap / 1024 / 1024 / T3_MB_PER_TEST_WORKER))
    if [ "$allowed" -lt 1 ]; then
        # A cap below one worker's footprint still needs ONE worker: 0 would leave the
        # lane with no runner at all, turning a tight box into a wedged one.
        allowed=1
    fi

    local cores
    if [ -n "$T3_CPU_COUNT" ]; then
        cores=$T3_CPU_COUNT
    else
        cores=$(nproc 2>/dev/null || echo 1)
    fi
    if [ "$allowed" -ge "$cores" ]; then
        # Memory is not the binding constraint here; leave `-n auto` to the cores.
        return 0
    fi

    export PYTEST_XDIST_AUTO_NUM_WORKERS="$allowed"
    echo "=== bounding pytest to ${allowed} worker(s): cgroup memory cap $((cap / 1024 / 1024)) MiB at ${T3_MB_PER_TEST_WORKER} MiB/worker (${cores} cores) ==="
    return 0
}
