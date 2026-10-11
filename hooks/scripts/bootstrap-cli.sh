#!/usr/bin/env bash

# SessionStart hook: ensure the t3 CLI is available.
#
# This hook does what its name says and nothing more, and it must never block
# session start.
#
# It used to call `t3 doctor check` synchronously. `t3` is containerized and
# `doctor check` sweeps every check, so that call costs minutes; on a real
# session it made SessionStart take 13 MINUTES before a first prompt could be typed, and it sent its output to /dev/null, so the
# whole cost bought a result nobody ever saw. It was also the only one of 18
# hook registrations carrying no `timeout`, so nothing bounded it.
#
# Configuration verification belongs in CI, not on the session-start path:
# doctor's own checks are covered by tests/cli_doctor/, and
# tests/test_hooks_json_declare_timeouts.py pins that no hook is unbounded.
# Whether the teatree MCP is live is per-machine and per-session, so it is
# checked REACTIVELY -- when an MCP tool actually fails -- never as a
# session-start ritual. Session start stays instant.
# It also warns, with no MCP call, when a Docker install has no python3 for the MCP stdio proxy.

set -u

# Bounded, so a symlink cycle cannot stall session start; `readlink -f` is missing on older macOS.
runs_deploy_wrapper() {
    local path="$1" hops=0 link
    while [ -L "$path" ] && [ "$hops" -lt 8 ]; do
        link="$(readlink "$path")" || return 1
        case "$link" in /*) path="$link" ;; *) path="${path%/*}/$link" ;; esac
        hops=$((hops + 1))
    done
    case "$path" in */deploy/t3) return 0 ;; esac
    grep -qF '# >>> teatree docker t3 launcher >>>' "$path" 2>/dev/null
}

T3_PATH="$(command -v t3 2>/dev/null)" || T3_PATH=""
if [ -z "$T3_PATH" ]; then
    printf 't3 CLI not found on PATH; teatree commands will not work in this session.\n' >&2
elif runs_deploy_wrapper "$T3_PATH" &&
    ! python3 -I -S -c 'import sys; sys.exit(sys.version_info < (3, 9))' >/dev/null 2>&1; then
    # stdout, not stderr: a SessionStart hook's stdout is what reaches the session.
    printf 'WARN teatree: this host has no python3 >= 3.9, so the teatree MCP tools will drop at the next worker restart and stay gone until the server is reconnected from the /mcp menu. Install python3 to keep them across restarts.\n'
fi

exit 0
