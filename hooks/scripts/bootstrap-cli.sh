#!/usr/bin/env bash

# SessionStart hook: ensure the t3 CLI is available.
#
# This hook does what its name says and nothing more, and it must never block
# session start.
#
# It used to call `t3 doctor check` synchronously. `t3` is containerized and
# `doctor check` live-probes every enabled MCP connector, so that call costs
# minutes; on a real session it made SessionStart take 13 MINUTES before a
# first prompt could be typed, and it sent its output to /dev/null, so the
# whole cost bought a result nobody ever saw. It was also the only one of 18
# hook registrations carrying no `timeout`, so nothing bounded it.
#
# Connector and configuration verification belongs in CI, not on the
# session-start path: doctor's own checks are covered by tests/cli_doctor/, and
# tests/test_hooks_json_declare_timeouts.py pins that no hook is unbounded.
# Live MCP connectivity is inherently per-machine and per-session, so it is
# checked REACTIVELY -- when an MCP tool actually fails -- never as a
# session-start ritual. Session start stays instant.

set -u

if ! command -v t3 >/dev/null 2>&1; then
    printf 't3 CLI not found on PATH; teatree commands will not work in this session.\n' >&2
fi

exit 0
