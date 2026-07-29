"""Dispatch ``ScanSignal``s into actions: statusline notes or phase agents.

The dispatcher is the only place where the loop decides *what to do* with a
signal. Inline mechanical actions (lint fixes, n8n webhooks, statusline
notes) run here. Judgment calls are delegated to phase agents — the
dispatcher records the agent invocation as an ``"agent"`` action so the
runtime layer can spawn them via the standard Task tool.

The dispatcher does not invoke Claude; it only routes. The runtime that
hosts the loop (a Claude Code ``/loop`` slot) reads the action list and
spawns agents with the standard tool. This keeps unit tests deterministic.

The routing tables (``teatree.loop.dispatch_tables``), the pure routing
predicates (``teatree.loop.dispatch_reducer``), and the network/DB seams
(``teatree.loop.dispatch_gates``) live in sibling modules; this module owns
the *consult order* — the chain ``_dispatch_one`` walks for each signal and
the per-tick ``dispatch`` loop that swallows per-signal errors.
"""

import logging
from typing import TYPE_CHECKING

from teatree.loop.dispatch_gates import dispatch_incoming_task, dispatch_slack_message, gate_review_intent
from teatree.loop.dispatch_reducer import (
    codex_review_dispatch,
    dispatch_flag_no_review,
    dispatch_pending_task,
    is_statusline_dropped,
)
from teatree.loop.dispatch_tables import (
    AGENT_BY_KIND,
    DUAL_DISPATCH,
    MECHANICAL_BY_KIND,
    STATUSLINE_ZONE_BY_KIND,
    ActionKind,
    ActionPayload,
    DispatchAction,
)
from teatree.loop.scanners.base import ScanSignal

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

__all__ = [
    "ActionKind",
    "ActionPayload",
    "DispatchAction",
    "dispatch",
]


#: Per-kind payload-conditional handlers consulted before the generic lookups.
#: Each returns its action list or ``None`` to fall through (see
#: :func:`_conditional_dispatch`).
_CONDITIONAL_HANDLERS: dict[str, "Callable[[ScanSignal], list[DispatchAction] | None]"] = {
    "pending_task": dispatch_pending_task,
    "pr_sweep.flag_no_review": dispatch_flag_no_review,
    "slack.review_intent": gate_review_intent,
    "slack.mention": dispatch_slack_message,
    "slack.dm": dispatch_slack_message,
    "incoming_event.task_needed": dispatch_incoming_task,
    "codex_review.dispatch": codex_review_dispatch,
}


def _conditional_dispatch(signal: ScanSignal) -> list[DispatchAction] | None:
    """Payload-conditional special cases that precede the generic lookups.

    Each handler returns its action list, or ``None`` to fall through to the
    generic ``AGENT_BY_KIND`` / ``MECHANICAL_BY_KIND`` / statusline chain in
    ``_dispatch_one``. A per-kind dispatch table keeps this flat.
    """
    handler = _CONDITIONAL_HANDLERS.get(signal.kind)
    return handler(signal) if handler is not None else None


def _dispatch_one(signal: ScanSignal) -> list[DispatchAction]:
    conditional = _conditional_dispatch(signal)
    if conditional is not None:
        return conditional
    # A registered mechanical handler always wins over a statusline drop: a
    # signal whose prefix is in ``STATUSLINE_DROP_PREFIXES`` (e.g. the #2190
    # ``local_stack.*`` bookkeeping) must still reach its mechanical executor —
    # the drop only suppresses the *statusline fallback* rendering, not the
    # action. Checked before the drop so the reaper/drainer fire while their
    # signals stay off the statusline.
    mech = MECHANICAL_BY_KIND.get(signal.kind)
    if mech is not None:
        kind, zone = mech
        return [DispatchAction(kind=kind, zone=zone, detail=signal.summary, payload=signal.payload)]
    if is_statusline_dropped(signal):
        return []
    agent = AGENT_BY_KIND.get(signal.kind)
    if agent is not None:
        actions: list[DispatchAction] = []
        # The agent action is emitted unconditionally; the RedMrFixAttempt
        # idempotency claim for ``my_pr.failed`` (#1295 cap D) now lives at
        # PERSIST time (``persistence._handle_debug``, #1 blocker), so a dropped
        # persist rolls the claim back and the next tick retries — the claim can
        # no longer be burned before the Task is created.
        actions.append(
            DispatchAction(kind="agent", zone=agent, detail=signal.summary, payload=signal.payload),
        )
        if signal.kind in DUAL_DISPATCH:
            actions.append(
                DispatchAction(
                    kind="statusline",
                    zone=STATUSLINE_ZONE_BY_KIND.get(signal.kind, "in_flight"),
                    detail=signal.summary,
                    payload=signal.payload,
                ),
            )
        return actions
    if signal.kind == "ticket.disposition_candidate" and signal.payload.get("reason") == "issue_closed":
        return [
            DispatchAction(kind="mechanical", zone="ticket_disposition", detail=signal.summary, payload=signal.payload),
        ]
    zone = STATUSLINE_ZONE_BY_KIND.get(signal.kind, "in_flight")
    return [DispatchAction(kind="statusline", zone=zone, detail=signal.summary, payload=signal.payload)]


def dispatch(
    signals: list[ScanSignal],
    *,
    errors: dict[str, str] | None = None,
) -> list[DispatchAction]:
    """Turn a flat list of signals into the actions the runtime should perform.

    Slack mentions/DMs that carry a PR URL also emit a derived
    ``review_channel.request`` agent action — the URL extraction lives here
    rather than in a second scanner so the messaging backend is hit once
    per tick.

    Per-signal exceptions are swallowed so a bad handler never aborts the
    tick.  When *errors* is supplied (a ``TickReport.errors`` dict), each
    swallowed exception is recorded under ``'dispatch:<signal.kind>'`` so it
    surfaces in the action-needed render.
    """
    actions: list[DispatchAction] = []
    for signal in signals:
        try:
            actions.extend(_dispatch_one(signal))
        except Exception as exc:
            logger.exception("Signal %s raised during dispatch", signal.kind)
            if errors is not None:
                errors[f"dispatch:{signal.kind}"] = f"{type(exc).__name__}: {exc}"
    return actions
