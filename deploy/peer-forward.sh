#!/usr/bin/env bash
# The host side of `t3 peer up|down|status` — the ONE place a peer's loopback forward is
# opened, reused, refused or closed. It executes on the operator's own machine because that
# is where the transport binaries and their credentials are, and because the near end of the
# forward must bind the loopback the dashboard's fetch actually means.
#
# It knows nothing about any box: every coordinate — the peer's label, its port, the whole
# command that opens it — arrives in the plan file the CLI resolved from the `peer_instances`
# registry. That is what keeps a host, a project, a zone or a key path in config and out of
# this repository.
#
# Usage: peer-forward.sh <plan-file>
#   line 1  action=up|down|status
#   line 2  wait_seconds=<float>
#   rows    <peer>\t<port>\t<command>          (command LAST — it is a whole command line)
set -uo pipefail

POLL_INTERVAL=0.5
# What teatree may adopt a port from without having opened it — glob patterns, matched with
# `case`. Only the transports it opens forwards with: anything else answering that port is
# someone's server or stray process, and reading a peer through it reaches that process
# instead of the box.
TUNNEL_COMMANDS="ssh gcloud"

# gcloud IS a python program, so lsof can name the interpreter rather than the wrapper the
# operator typed. That allowance is read off THIS peer's own command and is never blanket:
# blanket, it adopted any python listener at all — a local runserver on the peer's port was
# "reused", and the compare page then read THIS box under that peer's label.
GCLOUD_HOLDERS="python*"

marker_for() { echo "$STATE_DIR/$1.pid"; }
log_for() { echo "$STATE_DIR/$1.log"; }

# The command name of whatever holds 127.0.0.1:<port>. Empty means UNKNOWN, never "nothing" —
# lsof names no process for a port another user holds, and none at all when it is absent.
# `lsof -F c` prints one `c<name>` line per process; the first is enough.
holder_of() {
    command -v lsof >/dev/null 2>&1 || return 0
    lsof -nP -iTCP:"$1" -sTCP:LISTEN -F c 2>/dev/null | sed -n 's/^c//p' | head -1
}

# The transport binary a plan's command runs — its first word, stripped of any leading path.
transport_of() {
    local first
    # shellcheck disable=SC2086 # deliberate split: the transport is the command's first word.
    set -- $1
    first="${1:-}"
    echo "${first##*/}"
}

# Whether *holder* may be adopted as the forward for a peer whose plan runs *command*.
is_tunnel() {
    local holder="$1" command="${2:-}" known
    for known in $TUNNEL_COMMANDS; do
        case "$holder" in
        $known) return 0 ;;
        esac
    done
    [ "$(transport_of "$command")" = gcloud ] || return 1
    for known in $GCLOUD_HOLDERS; do
        case "$holder" in
        $known) return 0 ;;
        esac
    done
    return 1
}

# Bash's own /dev/tcp rather than nc, which is absent or incompatible often enough that
# depending on it would leave readiness unprovable on a host where the forward is fine.
answers() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

# The live process teatree started for this peer, if it is still running. This is what makes
# a forward OURS: the holder's name alone can never distinguish one we opened from one we
# merely found, and only the first may be closed.
owner_pid() {
    local marker pid
    marker="$(marker_for "$1")"
    [ -r "$marker" ] || return 1
    pid="$(cat "$marker" 2>/dev/null)"
    [ -n "$pid" ] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    echo "$pid"
}

polls_for() { awk -v w="$1" -v i="$POLL_INTERVAL" 'BEGIN { n = int(w / i); print (n < 1 ? 1 : n) }'; }

wait_for() {
    local port="$1" left
    left="$(polls_for "$2")"
    while [ "$left" -gt 0 ]; do
        answers "$port" && return 0
        sleep "$POLL_INTERVAL"
        left=$((left - 1))
    done
    answers "$port"
}

wait_until_free() {
    local port="$1" left=10
    while [ "$left" -gt 0 ]; do
        answers "$port" || return 0
        sleep "$POLL_INTERVAL"
        left=$((left - 1))
    done
    return 1
}

forward_up() {
    local peer="$1" port="$2" cmd="$3" holder pid
    if answers "$port"; then
        if owner_pid "$peer" >/dev/null; then
            echo "$peer: already up on 127.0.0.1:$port — teatree opened it, leaving it alone."
            return 0
        fi
        holder="$(holder_of "$port")"
        if [ -n "$holder" ] && is_tunnel "$holder" "$cmd"; then
            echo "$peer: already up on 127.0.0.1:$port (held by $holder) — reusing it."
            return 0
        fi
        # An unnameable holder is REFUSED, not adopted: lsof answers nothing for a port another
        # user holds and nothing when it is absent, and neither of those is "it is a tunnel".
        if [ -z "$holder" ]; then
            echo "$peer: refusing 127.0.0.1:$port — something answers there and I could not identify it." >&2
        else
            echo "$peer: refusing 127.0.0.1:$port — it is held by $holder, which opens no forward." >&2
        fi
        echo "        Reading the peer through it would reach that process instead of the box." >&2
        return 1
    fi
    mkdir -p "$STATE_DIR"
    # `sh -c` because the command is joined for a human to paste, so a `~` in a key path is
    # the shell's to expand; nohup because the forward has to outlive this wrapper.
    #
    # `</dev/null` because this runs inside the loop that READS the plan file on stdin: a
    # transport that reads stdin (gcloud prompts) otherwise swallows the rows after its own,
    # and the peers below it are skipped silently while the run exits naming only the first.
    nohup sh -c "$cmd" >>"$(log_for "$peer")" 2>&1 </dev/null &
    pid=$!
    echo "$pid" >"$(marker_for "$peer")"
    if wait_for "$port" "$WAIT_SECONDS"; then
        echo "$peer: up on 127.0.0.1:$port."
        return 0
    fi
    kill "$pid" 2>/dev/null
    rm -f "$(marker_for "$peer")"
    echo "$peer: the forward did not come up on 127.0.0.1:$port within ${WAIT_SECONDS}s." >&2
    echo "        Its output is at $(log_for "$peer")." >&2
    return 1
}

forward_down() {
    local peer="$1" port="$2" pid holder
    if ! answers "$port"; then
        rm -f "$(marker_for "$peer")"
        echo "$peer: already down."
        return 0
    fi
    if pid="$(owner_pid "$peer")"; then
        kill "$pid" 2>/dev/null
        wait_until_free "$port" || kill -9 "$pid" 2>/dev/null
        rm -f "$(marker_for "$peer")"
        echo "$peer: closed."
        return 0
    fi
    holder="$(holder_of "$port")"
    echo "$peer: refusing to close 127.0.0.1:$port — teatree did not open it." >&2
    echo "        It is held by ${holder:-a process lsof cannot name}; whoever opened it owns it." >&2
    return 1
}

forward_status() {
    local peer="$1" port="$2" holder owned=no
    owner_pid "$peer" >/dev/null && owned=yes
    if answers "$port"; then
        holder="$(holder_of "$port")"
        echo "$peer: up on 127.0.0.1:$port (held by ${holder:-a process lsof cannot name}, opened by teatree: $owned)"
        return 0
    fi
    echo "$peer: down — nothing answers 127.0.0.1:$port"
    return 1
}

run_plan() {
    local plan="$1" line peer port cmd action="" rc=0
    WAIT_SECONDS=25
    STATE_DIR="$(dirname "$plan")/peer-forwards"
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
        action=*)
            action="${line#action=}"
            continue
            ;;
        wait_seconds=*)
            WAIT_SECONDS="${line#wait_seconds=}"
            continue
            ;;
        "") continue ;;
        esac
        IFS=$'\t' read -r peer port cmd <<<"$line"
        case "$action" in
        up) forward_up "$peer" "$port" "$cmd" || rc=1 ;;
        down) forward_down "$peer" "$port" || rc=1 ;;
        status) forward_status "$peer" "$port" || rc=1 ;;
        *)
            echo "peer-forward: the plan names no action I know ($action)." >&2
            return 64
            ;;
        esac
    done <"$plan"
    return "$rc"
}

main() {
    if [ -z "${1:-}" ] || [ ! -r "${1:-}" ]; then
        echo "peer-forward: no readable plan file given." >&2
        return 64
    fi
    run_plan "$1"
}

# Sourced by the tests to reach one predicate at a time; executed by `t3 peer` and deploy/t3.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
    exit $?
fi
