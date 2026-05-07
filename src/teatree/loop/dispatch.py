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

ActionKind = Literal["statusline", "agent", "webhook"]
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


def dispatch(signals: list[ScanSignal]) -> list[DispatchAction]:
    """Turn a flat list of signals into the actions the runtime should perform.

    Slack mentions/DMs that carry a PR URL also emit a derived
    ``review_channel.request`` agent action — the URL extraction lives here
    rather than in a second scanner so the messaging backend is hit once
    per tick.
    """
    actions: list[DispatchAction] = []
    for signal in signals:
        if signal.kind in {"slack.mention", "slack.dm"}:
            pr_url = _slack_pr_url(signal)
            if pr_url:
                actions.append(
                    DispatchAction(
                        kind="agent",
                        zone="t3:reviewer",
                        detail=f"Review request: {pr_url}",
                        payload={"url": pr_url, **signal.payload},
                    )
                )
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
