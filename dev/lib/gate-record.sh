#!/usr/bin/env bash

_T3_PUSH_GATE_RECORD=""

start_push_gate_record() {
    _T3_PUSH_GATE_RECORD=$(git rev-parse --path-format=absolute --git-path t3-push-gate-run 2>/dev/null) || {
        echo "=== WARNING: could not resolve the per-worktree push-gate run record; continuing without diagnostics ===" >&2
        _T3_PUSH_GATE_RECORD=""
        return 0
    }
    printf 'started=%s\npid=%s\n' "$(date +%s)" "$$" >|"$_T3_PUSH_GATE_RECORD" || {
        echo "=== WARNING: could not write push-gate run record $_T3_PUSH_GATE_RECORD; continuing without diagnostics ===" >&2
        _T3_PUSH_GATE_RECORD=""
    }
    return 0
}

_append_push_gate_record() {
    [ -n "$_T3_PUSH_GATE_RECORD" ] || return 0
    printf '%s\n' "$1" >>"$_T3_PUSH_GATE_RECORD" || true
}

record_push_gate_lock() {
    _append_push_gate_record "lock=${T3_PUSH_GATE_LOCK_PATH:-unserialised}"
    _append_push_gate_record "lock_wait_s=${T3_PUSH_GATE_LOCK_WAIT_S:-0}"
}

record_push_gate_bound() {
    _append_push_gate_record "bound=${T3_XDIST_BOUND_SUMMARY:-unknown}"
}

record_push_gate_stage() {
    _append_push_gate_record "stage=$1"
}

finish_push_gate_record() {
    local rc="$1"
    _append_push_gate_record "finished=$(date +%s)"
    _append_push_gate_record "rc=$rc"
}
