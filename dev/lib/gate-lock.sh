#!/usr/bin/env bash

select_push_gate_lock_path() {
    local lock_path=""
    if [ -n "${T3_PUSH_GATE_LOCK:-}" ]; then
        lock_path="$T3_PUSH_GATE_LOCK"
    elif [ -d /host-tmp ] && [ -w /host-tmp ]; then
        lock_path="/host-tmp/t3-push-gate.lock"
    else
        lock_path="/tmp/t3-push-gate.lock"
    fi
    T3_PUSH_GATE_LOCK_PATH="$lock_path"
    export T3_PUSH_GATE_LOCK_PATH
}

acquire_push_gate_lock() {
    local lock_started=$SECONDS
    select_push_gate_lock_path
    local lock_path="$T3_PUSH_GATE_LOCK_PATH"

    # Never fall back to another path: a second lock file excludes nobody.
    if ! exec 9<>"$lock_path"; then
        T3_PUSH_GATE_LOCK_WAIT_S=$((SECONDS - lock_started))
        export T3_PUSH_GATE_LOCK_WAIT_S
        echo "=== push-gate: ABORTED: cannot open lock $lock_path (fix its permissions); stage 3 (the incremental sweep) did not run ===" >&2
        return 75
    fi

    local lock_rc=0
    T3_PUSH_GATE_HOLDER_PID="$$" python3 src/teatree/utils/push_gate_lock.py \
        "${T3_PUSH_GATE_LOCK_WAIT_SECONDS:-1500}" || lock_rc=$?
    T3_PUSH_GATE_LOCK_WAIT_S=$((SECONDS - lock_started))
    export T3_PUSH_GATE_LOCK_WAIT_S
    if [ "$lock_rc" -eq 0 ]; then
        return 0
    fi
    exec 9>&-
    # 75 is the helper's timed-out wait, already reported with its holder.
    if [ "$lock_rc" -ne 75 ]; then
        echo "=== push-gate: ABORTED: lock helper failed (rc=$lock_rc) for $lock_path; stage 3 (the incremental sweep) did not run ===" >&2
    fi
    return 75
}
