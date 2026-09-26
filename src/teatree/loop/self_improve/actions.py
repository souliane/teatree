"""Five-rung action ladder for self-improve firings (BLUEPRINT § 5.7).

Each ladder rung is a no-op upgrade over the previous one:

    ``log`` -> ``statusline`` -> ``slack`` -> ``ticket`` -> ``auto_fix``

Phase 1 only mechanically reaches ``statusline`` for every detector and
``slack`` for ``ForgottenMergeDetector``; ``ticket`` is plumbed but
gated by detector-declared ``max_rung``; ``auto_fix`` is whitelisted
(``StaleStatuslineEntryDetector`` only).  The full rung table:

    log         — durable row only; no UI surface
    statusline  — ``ScanSignal`` rendered in the action_needed zone
    slack       — self-DM via the active overlay's MessagingBackend
    ticket      — open an internal fix ticket and queue planning (no forge posting)
    auto_fix    — execute the detector's idempotent self-heal callable
"""

import hashlib
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.core.exceptions import ImproperlyConfigured
from django.db import transaction

from teatree.config import discover_active_overlay, discover_overlays
from teatree.core.models.self_improve_firing import SelfImproveFiring
from teatree.core.models.ticket import Ticket
from teatree.loop.persistence_phase_task import create_phase_task
from teatree.loop.self_improve.detectors.base import ActionRung, DetectorReport, fresh_or_escalated
from teatree.loop.self_improve.persistence import (
    SLACK_RATE_CAP_SECONDS,
    latest_firing,
    recent_slack_firings_within,
    record_firing,
)

if TYPE_CHECKING:
    from teatree.core.backend_protocols import MessagingBackend

logger = logging.getLogger(__name__)


_LADDER_ORDER: tuple[str, ...] = (
    ActionRung.LOG,
    ActionRung.STATUSLINE,
    ActionRung.SLACK,
    ActionRung.TICKET,
    ActionRung.AUTO_FIX,
)


@dataclass(frozen=True, slots=True)
class ActionResult:
    """The rung that ran, the firing row recorded, and any side-effect summary."""

    rung: str
    firing: SelfImproveFiring
    slack_capped: bool = False
    auto_fix_executed: bool = False


def _ceiling_index(report: DetectorReport) -> int:
    """Resolve ``max_rung`` (declared by the detector) to an index in ``_LADDER_ORDER``."""
    ceiling = report.max_rung
    if ceiling not in _LADDER_ORDER:
        return _LADDER_ORDER.index(ActionRung.STATUSLINE)
    return _LADDER_ORDER.index(ceiling)


def _next_rung_index(report: DetectorReport, firing: SelfImproveFiring | None, ceiling_index: int) -> int:
    """Compute the rung index for this observation.

    An ``auto_fix`` report targets its ceiling (``auto_fix``) on the FIRST
    firing: the self-heal is idempotent and side-effect-free, so it must run
    the moment the smell is observed rather than climb the graduated ladder.
    The climb would never reach ``auto_fix`` for a persistent smell anyway — the
    same stale state hashes to the same ``state_hash`` every tick, so
    ``fresh_or_escalated`` suppresses it at rung 1 and it can never escalate
    (the wired-but-unreachable bug #2625 Part B fixes).

    Other reports climb at least one rung when state changes. Detectors may
    request a higher non-auto-fix rung when the first observation already has
    enough evidence (e.g. 30 pressure denials before the next scan), bounded
    by the detector's ceiling.
    """
    if report.auto_fix:
        return ceiling_index
    requested = _LADDER_ORDER.index(report.requested_rung) if report.requested_rung in _LADDER_ORDER else 1
    if firing is None:
        return min(max(_LADDER_ORDER.index(ActionRung.STATUSLINE), requested), ceiling_index)
    current = _LADDER_ORDER.index(firing.last_action) if firing.last_action in _LADDER_ORDER else 0
    return min(max(current + 1, requested), ceiling_index)


def format_slack_payload(report: DetectorReport) -> dict[str, str]:
    """Build the Slack DM payload for a self-improve firing.

    One single location for the channel and the message shape so the
    post-#963 "switch to the bot channel" flip is a one-variable change.
    """
    channel = report.payload.get("slack_channel", "")
    text = (
        f"[self-improve] {report.detector}: {report.summary}\n"
        f"  severity: {report.severity}\n"
        f"  dedup_key: {report.dedup_key}"
    )
    return {"channel": str(channel), "text": text}


def _slack_capped(now_count: int) -> bool:
    """Has the global Slack rate cap been hit in the trailing window?"""
    return now_count >= 1


def owner_alert_key(report: DetectorReport, existing: SelfImproveFiring | None) -> str:
    """Give a recovered incident a new, bounded delivery identity."""
    kind = str(report.payload.get("kind") or report.detector).replace("_", "-")
    identity = hashlib.sha256(report.dedup_key.encode()).hexdigest()[:12]
    state = hashlib.sha256(report.state_hash.encode()).hexdigest()[:12]
    generation = str(int(existing.resolved_at.timestamp())) if existing and existing.resolved_at else "initial"
    return f"self-improve:{kind}:{identity}:{state}:{generation}"


def _post_to_backend(report: DetectorReport, messaging: "MessagingBackend") -> bool:
    payload = format_slack_payload(report)
    if not payload["channel"]:
        return False
    try:
        posted = messaging.post_message(channel=payload["channel"], text=payload["text"])
    except Exception:  # An alert transport failure must not kill the monitor.
        logger.exception("self-improve Slack delivery failed for %s", report.dedup_key)
        return False
    return posted.get("ok") is True and bool(posted.get("ts"))


def _slack_rung_outcome(
    report: DetectorReport,
    existing: SelfImproveFiring | None,
    messaging: "MessagingBackend | None",
    owner_alert: Callable[[DetectorReport, SelfImproveFiring | None], bool] | None,
) -> tuple[str, bool, bool]:
    """Return (actual rung, rate-capped, record firing); never claim a failed DM."""
    required = report.payload.get("requires_delivery") is True
    if _slack_capped(recent_slack_firings_within(SLACK_RATE_CAP_SECONDS)) and not required:
        return ActionRung.STATUSLINE, True, True
    delivered = (
        _post_to_backend(report, messaging)
        if messaging is not None
        else owner_alert(report, existing)
        if owner_alert is not None
        else False
    )
    if delivered:
        return ActionRung.SLACK, False, True
    return ActionRung.STATUSLINE, False, not required


def _repair_overlay_name(declared_name: str | None) -> str:
    available = {entry.name for entry in discover_overlays()}
    active = discover_active_overlay()
    name = declared_name or os.environ.get("T3_OVERLAY_NAME") or (active.name if active is not None else "")
    if not name and len(available) == 1:
        return next(iter(available))
    if not name:
        msg = f"Multiple overlays found ({', '.join(sorted(available))}). Declare a repair-ticket owner."
        raise ImproperlyConfigured(msg)
    if name not in available:
        msg = f"Overlay {name!r} not found. Available: {', '.join(sorted(available))}"
        raise ImproperlyConfigured(msg)
    return name


def _require_ticket_owner(ticket: Ticket, overlay_name: str) -> None:
    if ticket.overlay != overlay_name:
        msg = f"Self-improve firing already belongs to overlay {ticket.overlay!r}, not {overlay_name!r}"
        raise ImproperlyConfigured(msg)


def _record_ticket_followup(report: DetectorReport, *, overlay_name: str) -> SelfImproveFiring:
    """Create one internal fix ticket and a pending planning task for a ticket-rung smell.

    The pending task makes the finding actionable when capacity returns; the
    ordinary agent-dispatch/admission gates retain control of its execution.
    There is no forge post or direct automatic code change at this rung.
    """
    with transaction.atomic():
        firing = record_firing(report, action=ActionRung.TICKET)
        firing = SelfImproveFiring.objects.select_for_update().get(pk=firing.pk)
        existing_ticket = firing.ticket
        if existing_ticket is not None:
            _require_ticket_owner(existing_ticket, overlay_name)
        settled = Ticket.marker_release_states() | {Ticket.State.RETRO_RECORDED}
        if existing_ticket is not None and existing_ticket.state not in settled:
            if not existing_ticket.tasks.exists():
                create_phase_task(
                    existing_ticket,
                    phase="planning",
                    agent_id="self-improve",
                    reason=f"Investigate {report.detector}: {report.summary}; verify the pressure cause",
                )
            return firing
        suggested = report.payload.get("suggested_action")
        context = f"Self-improvement finding: {report.summary}\nDedup key: {report.dedup_key}"
        if isinstance(suggested, str) and suggested:
            context += f"\nSuggested action: {suggested}"
        ticket = Ticket.objects.create(
            overlay=overlay_name,
            kind=Ticket.Kind.FIX,
            short_description=f"Self-improve: {report.detector}"[:80],
            context=context,
            extra={"source": "self_improve"},
        )
        firing.ticket = ticket
        firing.save(update_fields=["ticket"])
        create_phase_task(
            ticket,
            phase="planning",
            agent_id="self-improve",
            reason=f"Investigate {report.detector}: {report.summary}; use telemetry and verify the cause before fixing",
        )
        return firing


def run_action_ladder(
    report: DetectorReport,
    *,
    overlay_name: str | None = None,
    messaging: "MessagingBackend | None" = None,
    owner_alert: Callable[[DetectorReport, SelfImproveFiring | None], bool] | None = None,
    auto_fix_callable: Callable[[DetectorReport], None] | None = None,
) -> ActionResult | None:
    """Advance the ladder for one detector report, bounded by its ceiling.

    Returns ``None`` when the firing is suppressed by cool-down (same
    ``state_hash`` as the last firing); otherwise records the new
    ``SelfImproveFiring`` row and executes the side effect for the
    resolved rung.

    The Slack rate cap is enforced before the messaging call: when the
    cap is hit the rung is downgraded to ``statusline`` and the firing
    row records ``slack_capped=True`` via ``last_action="statusline"``.
    """
    source_overlay = report.payload.get("overlay_name")
    if source_overlay is not None and source_overlay != _repair_overlay_name(overlay_name):
        msg = f"Self-improve evidence belongs to overlay {source_overlay!r}, not {overlay_name!r}"
        raise ImproperlyConfigured(msg)
    existing = latest_firing(report.detector, report.dedup_key)
    resolved_overlay = None
    linked_ticket = existing.ticket if existing is not None else None
    if isinstance(linked_ticket, Ticket):
        resolved_overlay = _repair_overlay_name(overlay_name)
        _require_ticket_owner(linked_ticket, resolved_overlay)
    if not fresh_or_escalated(report, existing):
        return None

    ceiling_index = _ceiling_index(report)
    prior = None if existing is not None and existing.resolved_at else existing
    rung_index = _next_rung_index(report, prior, ceiling_index)
    rung = _LADDER_ORDER[rung_index]

    slack_capped = False
    auto_fix_executed = False

    if rung == ActionRung.SLACK:
        rung, slack_capped, record = _slack_rung_outcome(report, existing, messaging, owner_alert)
        if not record:
            return None  # Keep failed required delivery retryable on the next tick.
    elif rung == ActionRung.AUTO_FIX:
        if not report.auto_fix:
            # Detector did not opt in to auto-fix — refuse to execute
            # even when the ceiling resolves there.  The structural test
            # enumerates this constraint.
            rung = ActionRung.STATUSLINE
        elif auto_fix_callable is not None:
            auto_fix_callable(report)
            auto_fix_executed = True

    firing = (
        _record_ticket_followup(report, overlay_name=resolved_overlay or _repair_overlay_name(overlay_name))
        if rung == ActionRung.TICKET
        else record_firing(report, action=rung)
    )
    return ActionResult(
        rung=rung,
        firing=firing,
        slack_capped=slack_capped,
        auto_fix_executed=auto_fix_executed,
    )
