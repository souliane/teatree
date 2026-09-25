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
# An explicit `PYTEST_XDIST_AUTO_NUM_WORKERS=4 bash dev/<lane>.sh` is a ceiling: the
# memory bound may lower it but never widen it. An uncapped box is left alone.

# The ticket's measured whole-tree workers used ~750 MiB RSS each. Round UP to
# 768 MiB so a 2 GiB cap budgets two workers, not the unsafe four at 512 MiB.
# Override to re-tune from a newer measurement without changing every lane.
: "${T3_MB_PER_TEST_WORKER:=768}"

# Withheld from the worker budget before it is divided. The pytest PARENT, Django's
# per-worker import and the container floor are not free, so budgeting the whole cap to
# workers overshoots it: measured on a 2048 MiB cap, 4 workers peaked at 2050 MiB and the
# cgroup killed the push gate, while 2 workers sat at 1305 MiB (#4589).
: "${T3_MB_PARENT_RESERVE:=512}"

# Injectable for tests; the defaults are the real cgroup v2 / v1 paths.
: "${T3_CGROUP_MEMORY_MAX_V2:=/sys/fs/cgroup/memory.max}"
: "${T3_CGROUP_MEMORY_CURRENT_V2:=/sys/fs/cgroup/memory.current}"
: "${T3_CGROUP_MEMORY_STAT_V2:=/sys/fs/cgroup/memory.stat}"
: "${T3_CGROUP_MEMORY_MAX_V1:=/sys/fs/cgroup/memory/memory.limit_in_bytes}"

# Core count, injectable for the same reason. Empty means "detect with nproc"; a value
# pins it, so a test can exercise the memory arithmetic without its result depending on
# how many cores the runner happens to have.
: "${T3_CPU_COUNT:=}"

# cgroup v1 reports a near-2**63 page-aligned sentinel to mean "unlimited"; cgroup v2
# uses the literal string "max". Both mean "no cap" and must not bound anything.
_T3_CGROUP_UNLIMITED_SENTINEL=9223372036854771712
_T3_RECLAIMABLE_STAT_KEYS="inactive_file slab_reclaimable"

# Echo the cgroup version and cap in bytes, or return non-zero when uncapped/unreadable.
_t3_cgroup_memory_cap() {
    local raw=""
    if [ -r "$T3_CGROUP_MEMORY_MAX_V2" ]; then
        raw=$(cat "$T3_CGROUP_MEMORY_MAX_V2" 2>/dev/null || true)
        case "$raw" in
            '' | max | *[!0-9]*) ;;
            *)
                if [ "$raw" -lt "$_T3_CGROUP_UNLIMITED_SENTINEL" ]; then
                    echo "2 $raw"
                    return 0
                fi
                ;;
        esac
    fi
    raw=""
    if [ -r "$T3_CGROUP_MEMORY_MAX_V1" ]; then
        raw=$(cat "$T3_CGROUP_MEMORY_MAX_V1" 2>/dev/null || true)
    fi
    case "$raw" in
        '' | max | *[!0-9]*) return 1 ;;
    esac
    if [ "$raw" -ge "$_T3_CGROUP_UNLIMITED_SENTINEL" ]; then
        return 1
    fi
    echo "1 $raw"
}

_t3_cgroup_v2_reclaimable_bytes() {
    local total=0 key value
    [ -r "$T3_CGROUP_MEMORY_STAT_V2" ] || {
        echo 0
        return 0
    }
    while read -r key value _; do
        case " $_T3_RECLAIMABLE_STAT_KEYS " in
            *" $key "*)
                case "$value" in
                    '' | *[!0-9]*) ;;
                    *) total=$((total + value)) ;;
                esac
                ;;
        esac
    done <"$T3_CGROUP_MEMORY_STAT_V2"
    echo "$total"
}

_t3_cgroup_v2_headroom_bytes() {
    local cap="$1" current reclaimable used headroom
    [ -r "$T3_CGROUP_MEMORY_CURRENT_V2" ] || return 1
    current=$(cat "$T3_CGROUP_MEMORY_CURRENT_V2" 2>/dev/null || true)
    case "$current" in
        '' | *[!0-9]*) return 1 ;;
    esac
    reclaimable=$(_t3_cgroup_v2_reclaimable_bytes)
    used=$((current - reclaimable))
    [ "$used" -ge 0 ] || used=0
    headroom=$((cap - used))
    [ "$headroom" -ge 0 ] || headroom=0
    echo "$headroom"
}

_t3_export_bound_summary() {
    T3_XDIST_BOUND_SUMMARY="workers=$1 cap_mib=$2 headroom_mib=$3 reserve_mib=$T3_MB_PARENT_RESERVE per_worker_mib=$T3_MB_PER_TEST_WORKER"
    export T3_XDIST_BOUND_SUMMARY
}

# Bound PYTEST_XDIST_AUTO_NUM_WORKERS from cgroup headroom when it is available.
bound_xdist_workers_to_memory() {
    local pin="${PYTEST_XDIST_AUTO_NUM_WORKERS:-}"
    _t3_export_bound_summary "${pin:-auto}" "uncapped" "unknown"

    local cap_record version cap
    cap_record=$(_t3_cgroup_memory_cap) || return 0
    read -r version cap <<<"$cap_record"

    local cap_mib=$((cap / 1024 / 1024))
    local headroom="$cap"
    if [ "$version" -eq 2 ]; then
        headroom=$(_t3_cgroup_v2_headroom_bytes "$cap") || headroom="$cap"
    fi
    local headroom_mib=$((headroom / 1024 / 1024))
    local budget=$((headroom_mib - T3_MB_PARENT_RESERVE))
    local allowed=0
    if [ "$budget" -ge "$T3_MB_PER_TEST_WORKER" ]; then
        allowed=$((budget / T3_MB_PER_TEST_WORKER))
    fi
    if [ "$allowed" -lt 1 ]; then
        _t3_export_bound_summary "refused" "$cap_mib" "$headroom_mib"
        echo "=== REFUSED pytest: cgroup headroom ${headroom_mib} MiB under cap ${cap_mib} MiB less a ${T3_MB_PARENT_RESERVE} MiB parent reserve cannot afford one ${T3_MB_PER_TEST_WORKER} MiB worker ===" >&2
        return 1
    fi

    if [ -n "$pin" ]; then
        case "$pin" in
            *[!0-9]*)
                _t3_export_bound_summary "$pin" "$cap_mib" "$headroom_mib"
                return 0
                ;;
        esac
        if [ "$pin" -gt "$allowed" ]; then
            export PYTEST_XDIST_AUTO_NUM_WORKERS="$allowed"
            echo "=== WARNING: lowering explicit pin ${pin} to ${allowed} worker(s): cgroup headroom ${headroom_mib} MiB under cap ${cap_mib} MiB less a ${T3_MB_PARENT_RESERVE} MiB parent reserve at ${T3_MB_PER_TEST_WORKER} MiB/worker ==="
        fi
        _t3_export_bound_summary "$PYTEST_XDIST_AUTO_NUM_WORKERS" "$cap_mib" "$headroom_mib"
        return 0
    fi

    local cores
    if [ -n "$T3_CPU_COUNT" ]; then
        cores=$T3_CPU_COUNT
    else
        cores=$(nproc 2>/dev/null || echo 1)
    fi
    if [ "$allowed" -ge "$cores" ]; then
        _t3_export_bound_summary "auto" "$cap_mib" "$headroom_mib"
        return 0
    fi

    export PYTEST_XDIST_AUTO_NUM_WORKERS="$allowed"
    echo "=== bounding pytest to ${allowed} worker(s): cgroup headroom ${headroom_mib} MiB under cap ${cap_mib} MiB less a ${T3_MB_PARENT_RESERVE} MiB parent reserve at ${T3_MB_PER_TEST_WORKER} MiB/worker (${cores} cores) ==="
    _t3_export_bound_summary "$allowed" "$cap_mib" "$headroom_mib"
    return 0
}
