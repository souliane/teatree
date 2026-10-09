#!/usr/bin/env bash

acquire_push_gate_lock() {
    local lock_started=$SECONDS
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

    if ! exec 9<>"$lock_path"; then
        T3_PUSH_GATE_LOCK_WAIT_S=$((SECONDS - lock_started))
        export T3_PUSH_GATE_LOCK_WAIT_S
        echo "=== push-gate: WARNING: cannot open lock $lock_path — running unserialised ===" >&2
        return 0
    fi

    local lock_rc=0
    T3_PUSH_GATE_HOLDER_PID="$$" python3 src/teatree/utils/push_gate_lock.py \
        "${T3_PUSH_GATE_LOCK_WAIT_SECONDS:-1500}" || lock_rc=$?
    T3_PUSH_GATE_LOCK_WAIT_S=$((SECONDS - lock_started))
    export T3_PUSH_GATE_LOCK_WAIT_S
    if [ "$lock_rc" -eq 75 ]; then
        return 75
    fi
    if [ "$lock_rc" -ne 0 ]; then
        exec 9>&-
        echo "=== push-gate: WARNING: lock guard failed (rc=$lock_rc) for $lock_path — running unserialised ===" >&2
        return 0
    fi
}
