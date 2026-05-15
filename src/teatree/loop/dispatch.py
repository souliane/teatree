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

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from teatree.loop.scanners.base import ScanSignal

ActionKind = Literal["statusline", "agent", "webhook", "mechanical"]
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
    "reviewer_pr.approval_dismissed": "t3:reviewer",
    "pending_task": "t3:orchestrator",
}

_STATUSLINE_ZONE_BY_KIND: dict[str, str] = {
    "my_pr.failed": "action_needed",
    "my_pr.draft_notes": "action_needed",
    "my_pr.open": "in_flight",
    "slack.mention": "action_needed",
    "slack.dm": "action_needed",
    "assigned_issue.ready": "action_needed",
    "ticket.active": "anchors",
    "ticket.disposition_candidate": "action_needed",
    # Reviewer-assigned PRs also dispatch to the t3:reviewer agent below,
    # but we mirror them into the statusline so the user sees what's
    # pending review without waiting on the agent to act.
    "reviewer_pr.new_sha": "action_needed",
    "reviewer_pr.unreviewed": "action_needed",
    "reviewer_pr.approval_dismissed": "action_needed",
    # Inbound webhook events (#669). `recorded` is the passive status-update
    # case — relegated to in_flight so a noisy CI doesn't flood action_needed.
    "incoming_event.alert": "action_needed",
    "incoming_event.task_needed": "action_needed",
    "incoming_event.merge_needed": "action_needed",
    "incoming_event.recorded": "in_flight",
}

_PR_URL_RE = re.compile(r"https?://[^\s>|]+/(?:merge_requests|pull|pulls)/\d+")


def _slack_pr_url(signal: ScanSignal) -> str:
    """Extract a PR URL from a slack.mention/dm signal if its text contains one."""
    event = signal.payload.get("event")
    if not isinstance(event, dict):
        return ""
    text = event.get("text")
    if not isinstance(text, str):
        return ""
    match = _PR_URL_RE.search(text)
    return match.group(0) if match else ""


# Reviewer signals dispatch to the agent AND mirror into the statusline so
# the user sees the pending review before the agent acts.
_DUAL_DISPATCH: frozenset[str] = frozenset(
    {"reviewer_pr.new_sha", "reviewer_pr.unreviewed", "reviewer_pr.approval_dismissed"},
)

# Signal kind → (DispatchAction kind, zone) when the side-effect is a
# fire-and-forget mechanical handler or webhook rather than an agent.
_MECHANICAL_BY_KIND: dict[str, tuple[ActionKind, str]] = {
    "ticket.completion_detected": ("mechanical", "ticket_completion"),
    "ticket.reopen_needed": ("mechanical", "ticket_reopen"),
    "notion.unrouted": ("webhook", "n8n"),
}


def _dispatch_one(signal: ScanSignal) -> list[DispatchAction]:
    if signal.kind in {"slack.mention", "slack.dm"}:
        pr_url = _slack_pr_url(signal)
        if pr_url:
            return [
                DispatchAction(
                    kind="agent",
                    zone="t3:reviewer",
                    detail=f"Review request: {pr_url}",
                    payload={"url": pr_url, **signal.payload},
                ),
                DispatchAction(
                    kind="statusline",
                    zone=_STATUSLINE_ZONE_BY_KIND[signal.kind],
                    detail=signal.summary,
                    payload=signal.payload,
                ),
            ]
    if signal.kind == "assigned_issue.ready" and signal.payload.get("auto_start") is True:
        return [DispatchAction(kind="agent", zone="t3:orchestrator", detail=signal.summary, payload=signal.payload)]
    agent = _AGENT_BY_KIND.get(signal.kind)
    if agent is not None:
        actions = [DispatchAction(kind="agent", zone=agent, detail=signal.summary, payload=signal.payload)]
        if signal.kind in _DUAL_DISPATCH:
            actions.append(
                DispatchAction(
                    kind="statusline",
                    zone=_STATUSLINE_ZONE_BY_KIND.get(signal.kind, "in_flight"),
                    detail=signal.summary,
                    payload=signal.payload,
                ),
            )
        return actions
    if signal.kind == "ticket.disposition_candidate" and signal.payload.get("reason") == "issue_closed":
        return [
            DispatchAction(kind="mechanical", zone="ticket_disposition", detail=signal.summary, payload=signal.payload),
        ]
    mech = _MECHANICAL_BY_KIND.get(signal.kind)
    if mech is not None:
        kind, zone = mech
        return [DispatchAction(kind=kind, zone=zone, detail=signal.summary, payload=signal.payload)]
    zone = _STATUSLINE_ZONE_BY_KIND.get(signal.kind, "in_flight")
    return [DispatchAction(kind="statusline", zone=zone, detail=signal.summary, payload=signal.payload)]


def dispatch(signals: list[ScanSignal]) -> list[DispatchAction]:
    """Turn a flat list of signals into the actions the runtime should perform.

    Slack mentions/DMs that carry a PR URL also emit a derived
    ``review_channel.request`` agent action — the URL extraction lives here
    rather than in a second scanner so the messaging backend is hit once
    per tick.
    """
    actions: list[DispatchAction] = []
    for signal in signals:
        actions.extend(_dispatch_one(signal))
    return actions
