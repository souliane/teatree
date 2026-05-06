"""Dispatch ``ScanSignal``s into actions: statusline notes or phase agents.

The dispatcher is the only place where the loop decides *what to do* with a
signal. Inline mechanical actions (lint fixes, n8n webhooks, statusline
notes) run here. Judgment calls are delegated to phase agents — the
dispatcher records the agent invocation as an ``"agent"`` action so the
runtime layer can spawn them via the standard Task tool.

The dispatcher does not invoke Claude; it only routes. The runtime that
hosts the loop (a Claude Code ``/loop`` slot) reads the action list and
spawns agents with the standard tool. This keeps unit tests deterministic.
"""

from dataclasses import dataclass, field
from typing import Any, Literal

from teatree.loop.scanners.base import ScanSignal

ActionKind = Literal["statusline", "agent", "webhook", "ticket_create"]
type ActionPayload = dict[str, Any]


@dataclass(frozen=True, slots=True)
class DispatchAction:
    """One side-effect produced by the dispatcher for a tick."""

    kind: ActionKind
    zone: str  # statusline zone or agent name (depending on kind)
    detail: str
    payload: ActionPayload = field(default_factory=dict)


_AGENT_BY_KIND: dict[str, str] = {
    "reviewer_pr.new_sha": "t3:reviewer",
    "reviewer_pr.unreviewed": "t3:reviewer",
    "review_channel.request": "t3:reviewer",
    "pending_task": "t3:orchestrator",
    "assigned_issue.ready": "t3:orchestrator",
}

_STATUSLINE_ZONE_BY_KIND: dict[str, str] = {
    "my_pr.failed": "action_needed",
    "my_pr.draft_notes": "action_needed",
    "my_pr.open": "in_flight",
    "slack.mention": "action_needed",
    "slack.dm": "action_needed",
}


def dispatch(signals: list[ScanSignal]) -> list[DispatchAction]:
    """Turn a flat list of signals into the actions the runtime should perform."""
    actions: list[DispatchAction] = []
    for signal in signals:
        agent = _AGENT_BY_KIND.get(signal.kind)
        if agent is not None:
            actions.append(DispatchAction(kind="agent", zone=agent, detail=signal.summary, payload=signal.payload))
            continue
        if signal.kind == "notion.unrouted":
            actions.append(DispatchAction(kind="webhook", zone="n8n", detail=signal.summary, payload=signal.payload))
            continue
        zone = _STATUSLINE_ZONE_BY_KIND.get(signal.kind, "in_flight")
        actions.append(DispatchAction(kind="statusline", zone=zone, detail=signal.summary, payload=signal.payload))
    return actions
