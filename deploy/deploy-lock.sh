#!/usr/bin/env bash
# The mkdir deploy lock a host without flock (macOS) falls back to, shared by deploy.sh and roll.sh.
# The caller sets DEPLOY_LOCK, DEPLOY_LOCK_MAX_AGE_MINUTES and DEPLOY_LOCK_MAX_RECLAIMS.

# `kill -0` cannot answer this alone: it fails with EPERM on ANOTHER USER's live
# process, and the default lock sits in world-shared /tmp, so a refused signal is
# evidence the process exists. `ps -p` reports existence without needing that right.
_pid_alive() {
    kill -0 "$1" 2>/dev/null || ps -p "$1" >/dev/null 2>&1
}

# The pid a lock names, or failure when it names none. `mkdir` publishes the lock one
# syscall BEFORE the pid lands in it, so an absent — or half-written — pid is a winner
# mid-acquisition, never a free lock; the caller must read failure here as HELD.
_lock_holder_pid() {
    local pid
    pid="$(cat "$1/pid" 2>/dev/null || true)"
    case "$pid" in
        '' | *[!0-9]*) return 1 ;;
    esac
    printf '%s' "$pid"
}

_lock_expired() {
    [ -n "$(find "$1" -maxdepth 0 -mmin "+$DEPLOY_LOCK_MAX_AGE_MINUTES" 2>/dev/null)" ]
}

# Judge "$DEPLOY_LOCK_DIR": fails while a live convergence holds it (its pid in DEPLOY_LOCK_HOLDER);
# otherwise DEPLOY_LOCK_RECLAIM says why it may be reclaimed, or is empty when it has gone.
_judge_deploy_lock() {
    local holder=""
    DEPLOY_LOCK_RECLAIM=""
    DEPLOY_LOCK_HOLDER=""
    if holder="$(_lock_holder_pid "$DEPLOY_LOCK_DIR")" && ! _pid_alive "$holder"; then
        DEPLOY_LOCK_RECLAIM="from dead pid $holder."
    elif _lock_expired "$DEPLOY_LOCK_DIR"; then
        DEPLOY_LOCK_RECLAIM="— pid ${holder:-unknown} has held it over ${DEPLOY_LOCK_MAX_AGE_MINUTES}m, longer than a convergence can run."
    elif [ -d "$DEPLOY_LOCK_DIR" ]; then
        DEPLOY_LOCK_HOLDER="${holder:-unknown}"
        return 1
    fi
}

# One reclaimer at a time, so none removes a lock another has just taken. A symlink carries its owner's
# pid in the one syscall that creates it, so a mutex whose pid died, or a minute old, is abandoned.
_take_reclaim_mutex() {
    local mutex="$DEPLOY_LOCK.reclaim" owner
    ln -s "$$" "$mutex" 2>/dev/null && return 0
    owner="$(readlink "$mutex" 2>/dev/null || true)"
    [ "$owner" != "$$" ] || return 0
    if [ -z "$owner" ] || ! _pid_alive "$owner" || [ -n "$(find "$mutex" -maxdepth 0 -mmin +1 2>/dev/null)" ]; then
        rm -f "$mutex"
    fi
    return 1
}

_drop_reclaim_mutex() {
    [ "$(readlink "$DEPLOY_LOCK.reclaim" 2>/dev/null || true)" != "$$" ] || rm -f "$DEPLOY_LOCK.reclaim"
}

# Take "$DEPLOY_LOCK.d", reclaiming it from a dead pid or from a holder past the age no
# convergence can reach. <who> prefixes every line it prints. Returns 0 once held, 1 while a
# live convergence holds it (its pid in DEPLOY_LOCK_HOLDER), 2 when it cannot be reclaimed.
acquire_deploy_lock_dir() {
    local who="$1" reclaims=0
    DEPLOY_LOCK_DIR="$DEPLOY_LOCK.d"
    until mkdir "$DEPLOY_LOCK_DIR" 2>/dev/null; do
        if [ "$reclaims" -ge "$DEPLOY_LOCK_MAX_RECLAIMS" ]; then
            echo "$who: $DEPLOY_LOCK_DIR keeps reappearing after $DEPLOY_LOCK_MAX_RECLAIMS reclaims — refusing to converge." >&2
            return 2
        fi
        _judge_deploy_lock || return 1
        if [ -n "$DEPLOY_LOCK_RECLAIM" ]; then
            # Waiting on a live reclaimer spends no reclaim: the mutex is abandoned within a minute anyway.
            if ! _take_reclaim_mutex; then
                sleep 1
                continue
            fi
            # Judged again under the mutex: the lock judged stale may already have been replaced.
            if ! _judge_deploy_lock; then
                _drop_reclaim_mutex || true
                return 1
            fi
            if [ -n "$DEPLOY_LOCK_RECLAIM" ]; then
                echo "$who: reclaiming $DEPLOY_LOCK_DIR $DEPLOY_LOCK_RECLAIM" >&2
                if ! rm -rf "$DEPLOY_LOCK_DIR"; then
                    _drop_reclaim_mutex || true
                    echo "$who: cannot remove the stale $DEPLOY_LOCK_DIR — refusing to converge." >&2
                    return 2
                fi
            fi
            _drop_reclaim_mutex || true
        fi
        reclaims=$((reclaims + 1))
        sleep 1
    done
    printf '%s\n' "$$" >"$DEPLOY_LOCK_DIR/pid"
}

# Release ONLY a lock this process is named in. Ownership is proved from the lock
# itself, not from where the trap sits relative to the acquisition, so hoisting the
# trap — its other job has nothing to do with the lock — cannot make it delete the
# lock another convergence holds.
_release_deploy_lock() {
    [ -n "${DEPLOY_LOCK_DIR:-}" ] || return 0
    [ "$(cat "$DEPLOY_LOCK_DIR/pid" 2>/dev/null || true)" = "$$" ] || return 0
    rm -rf "$DEPLOY_LOCK_DIR"
}
