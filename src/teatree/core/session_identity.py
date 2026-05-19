"""The active Claude session id — the loop's session-scoped identity (#1073).

Loop ownership is keyed by the *Claude session* that started the loop, NOT
the OS pid: ``loop_tick`` re-acquires the per-tick mutex with a fresh
``pid-<pid>`` every tick, so a pid-keyed identity rests unowned between
ticks and any session running ``t3 loop tick`` would win the unowned CAS
and do loop work (the #1073 hijack). The session id is stable across a
session's ticks and across context compaction, so it is the correct
identity for the persistent ``loop-owner`` claim.

This is the single source of truth for that primitive; both
``teatree.loop.session_identity`` (the loop-side callers) and
``teatree.outbound_claim`` (``_resolve_agent_session_id``) re-export it.
It lives in ``teatree.core`` rather than ``teatree.loop`` because the
module-boundary graph forbids ``teatree.outbound_claim`` (core-only)
depending on ``teatree.loop``; ``core`` is the lowest common module both
re-exporters are allowed to reach.
"""

import os


def current_session_id() -> str:
    """The active Claude session id, or ``""`` when not running under one.

    ``CLAUDE_SESSION_ID`` is set by Claude Code; ``T3_LOOP_SESSION_ID`` is
    the test/manual override. Empty string means anonymous (no session) —
    the loop-owner gate treats an anonymous caller as a non-owner whenever
    a live owner exists.
    """
    return os.environ.get("CLAUDE_SESSION_ID") or os.environ.get("T3_LOOP_SESSION_ID") or ""


__all__ = ["current_session_id"]
