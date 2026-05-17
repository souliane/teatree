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

from teatree.config import get_effective_settings
from teatree.core.phases import normalize_phase
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
    "ticket.stale": "action_needed",
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
    "incoming_event.merge_blocked": "action_needed",
    "incoming_event.merge_escalation": "action_needed",
    "incoming_event.recorded": "in_flight",
}

_PR_URL_RE = re.compile(r"https?://[^\s>|]+/(?:merge_requests|pull|pulls)/\d+")


def _first_pr_url(*texts: str) -> str:
    """Return the first PR/MR URL found across *texts*, or ``""``."""
    for text in texts:
        match = _PR_URL_RE.search(text)
        if match:
            return match.group(0)
    return ""


def _slack_pr_url(signal: ScanSignal) -> str:
    """Extract a PR URL from a slack.mention/dm signal if its text contains one."""
    event = signal.payload.get("event")
    if not isinstance(event, dict):
        return ""
    text = event.get("text")
    if not isinstance(text, str):
        return ""
    return _first_pr_url(text)


def _task_pr_url(signal: ScanSignal) -> str:
    """Extract a PR URL from an ``incoming_event.task_needed`` signal.

    The webhook path (``/hooks/slack/`` → IncomingEvent → classifier →
    router → scanner) puts the inbound body in ``payload['detail']`` and
    echoes it into ``summary``. A Slack message like "can you review
    https://…/merge_requests/42" classifies as ``TASK`` (the imperative
    "review" keyword) and would otherwise drop to a passive statusline
    note — the referenced PR never gets an independent review (#219).
    """
    detail = signal.payload.get("detail")
    detail_text = detail if isinstance(detail, str) else ""
    return _first_pr_url(detail_text, signal.summary)


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


def _dispatch_answering(signal: ScanSignal) -> list[DispatchAction]:
    """Route an ``answering``-phase task to the ``t3:answerer`` skill (#670).

    Mirrors the reviewer dual-dispatch: the inbound question becomes a
    ``t3:answerer`` agent invocation plus a statusline mirror so the user
    sees the pending answer before the agent acts. The autonomy level
    (``require_human_approval_to_answer``) is resolved here through the
    standard active-overlay → global → default chain (mirrors
    ``require_human_approval_to_merge``) and stamped into the agent
    payload as an advisory convenience mirror; the answerer skill
    re-resolves the setting at task start (see ``skills/answerer/SKILL.md``
    § Autonomy Gate), so the stamp is a hint, not the source of truth.
    ``coding``-phase task_needed signals are left to the statusline
    fallback — auto ticket creation from inbound chat is a separate
    decision pass (see ``IncomingEventsScanner``).
    """
    require_approval = get_effective_settings().require_human_approval_to_answer
    payload: ActionPayload = {**signal.payload, "require_human_approval_to_answer": require_approval}
    return [
        DispatchAction(kind="agent", zone="t3:answerer", detail=signal.summary, payload=payload),
        DispatchAction(
            kind="statusline",
            zone=_STATUSLINE_ZONE_BY_KIND.get(signal.kind, "action_needed"),
            detail=signal.summary,
            payload=signal.payload,
        ),
    ]


def _conditional_dispatch(signal: ScanSignal) -> list[DispatchAction] | None:
    """Payload-conditional special cases that precede the generic lookups.

    Returns ``None`` when no special case matches so ``_dispatch_one`` falls
    through to the ``_AGENT_BY_KIND`` / ``_MECHANICAL_BY_KIND`` / statusline
    chain. Keeping these here keeps ``_dispatch_one`` flat.
    """
    if signal.kind in {"slack.mention", "slack.dm"}:
        pr_url = _slack_pr_url(signal)
        if pr_url:
            return _review_request_dispatch(signal, pr_url)
    if signal.kind == "incoming_event.task_needed":
        pr_url = _task_pr_url(signal)
        if pr_url:
            return _review_request_dispatch(signal, pr_url)
    if signal.kind == "assigned_issue.ready" and signal.payload.get("auto_start") is True:
        return [DispatchAction(kind="agent", zone="t3:orchestrator", detail=signal.summary, payload=signal.payload)]
    phase = normalize_phase(str(signal.payload.get("phase", "")))
    if signal.kind == "incoming_event.task_needed" and phase == "answering":
        return _dispatch_answering(signal)
    return None


def _review_request_dispatch(signal: ScanSignal, pr_url: str) -> list[DispatchAction]:
    """Dual-dispatch a Slack review request to the reviewer agent.

    Shared by the polling path (``slack.mention``/``slack.dm``) and the
    webhook path (``incoming_event.task_needed`` carrying a PR URL, #219):
    an independent ``t3:reviewer`` invocation plus a statusline mirror so
    the user sees the pending review before the agent acts. A review
    request is a review request regardless of the classifier's phase —
    this branch precedes the ``answering`` fallback so "can you review
    MR X" routes to a review, not the answerer.
    """
    return [
        DispatchAction(
            kind="agent",
            zone="t3:reviewer",
            detail=f"Review request: {pr_url}",
            payload={"url": pr_url, **signal.payload},
        ),
        DispatchAction(
            kind="statusline",
            zone=_STATUSLINE_ZONE_BY_KIND.get(signal.kind, "action_needed"),
            detail=signal.summary,
            payload=signal.payload,
        ),
    ]


def _dispatch_one(signal: ScanSignal) -> list[DispatchAction]:
    conditional = _conditional_dispatch(signal)
    if conditional is not None:
        return conditional
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
