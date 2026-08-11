"""SubagentStop: hand a terminating sub-agent's admission seat back (#4129).

An admitted interactive dispatch takes a durable seat
(:class:`~teatree.core.models.InteractiveDispatch`) at the ``PreToolUse`` gate, because
it creates no ``Task`` row and was otherwise invisible to the very ceiling it had just
cleared. This is that seat's ordinary end.

Without it the seat could only ever lapse on
:data:`~teatree.core.models.SEAT_WINDOW`, so the ceiling would bound a dispatch RATE
rather than the live population it is written to bound: four quick sub-agents would hold
the lane shut long after the last of them exited, and a window short enough to avoid that
would forget agents that are still running.

Its own module rather than a third handler in the admission gate: that gate answers
"admit or deny" on ``PreToolUse``/``TaskCreated``, this rides a different event and
denies nothing. The router is shrink-only, so a new handler goes in a bare sibling
(``hooks/CLAUDE.md``).

Cold-import safe: stdlib only at module top — never Django / ``teatree.core``.
"""

import sys

# Alias the bare and ``hooks.scripts.`` identities so the handler the router
# re-exports and a test patching a helper here operate on ONE module object.
sys.modules.setdefault("dispatch_seat_release", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.dispatch_seat_release", sys.modules[__name__])


def handle_subagent_stop_release(data: dict) -> None:
    """Release the terminating sub-agent's seat, so the lane re-opens when it really is free.

    The main agent's own ``Stop`` carries no ``agent_id`` and releases nothing. Keyed on
    that id purely for idempotency — the seat is taken before the harness has assigned
    one, so no seat can be bound to its agent at birth.

    Best-effort: an unbootstrappable Django, a release failure, or any internal error
    leaves the seat to its window rather than propagating out of ``SubagentStop``.
    """
    try:
        session_id = str(data.get("session_id") or "").strip()
        agent_id = str(data.get("agent_id") or "").strip()
        if not session_id or not agent_id:
            return
        from hooks.scripts.django_bootstrap import bootstrap_teatree_django  # noqa: PLC0415 — deferred: cold-hook

        if not bootstrap_teatree_django():
            return
        from teatree.core.dispatch_admission import release_interactive_dispatch  # noqa: PLC0415 — deferred: post-setup

        release_interactive_dispatch(session_id=session_id, agent_id=agent_id)
    except Exception:  # noqa: BLE001 — crash-proof hook: a release bug must never break SubagentStop
        return
