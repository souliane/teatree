"""Explicit Slack delivery for owner questions during a check-in."""

from datetime import timedelta
from typing import TypedDict

from django.utils import timezone

from teatree.core.backend_protocols import MessagingBackend
from teatree.core.checking_status import plain_title
from teatree.core.models import BotPing, DeferredQuestion, IncomingEvent
from teatree.core.models.scanner_fault import ScannerFault
from teatree.core.notify import resolve_owner_dm_backend
from teatree.core.notify_question_drains import drain_unmirrored_deferred_questions, reask_escalated_questions
from teatree.utils.slack_links import slack_permalink

_FAULT_KEY = "checking:slack"


class ShootCardDict(TypedDict):
    label: str
    permalink: str


class ShootReportDict(TypedDict):
    mode: str
    reason: str
    posted: int
    nudged: int
    remaining: int
    cards: list[ShootCardDict]


def _broken_reason(backend: MessagingBackend | None, *, listener_stale: bool) -> str:
    if backend is None:
        return "Slack post path is unavailable"
    if listener_stale:
        return "Slack answer listener is stale"
    stuck_answer = IncomingEvent.objects.filter(
        source=IncomingEvent.Source.SLACK,
        processed_at__isnull=True,
        dead_lettered_at__isnull=True,
        received_at__lt=timezone.now() - timedelta(minutes=2),
    ).exists()
    if stuck_answer:
        return "Slack owner answers are not being processed"
    latest = BotPing.objects.filter(kind=BotPing.Kind.QUESTION).order_by("-posted_at", "-pk").first()
    if (
        latest
        and latest.status in {BotPing.Status.FAILED, BotPing.Status.NOOP}
        and latest.posted_at > timezone.now() - timedelta(hours=24)
    ):
        return "Slack question post failed"
    return ""


def _linked_cards() -> list[ShootCardDict]:
    cards: list[ShootCardDict] = []
    for row in DeferredQuestion.owner_pending().select_related("parked_task__ticket"):
        permalink = slack_permalink(row.slack_channel, row.slack_ts)
        if not permalink:
            continue
        ticket = row.parked_task.ticket if row.parked_task else None
        label = plain_title(ticket.short_description) if ticket and ticket.short_description else "a waiting question"
        cards.append({"label": label, "permalink": permalink})
    return cards


def shoot_questions(*, overlay: str, listener_stale: bool) -> ShootReportDict:
    backend, _reason = resolve_owner_dm_backend()
    broken = _broken_reason(backend, listener_stale=listener_stale)
    if broken:
        ScannerFault.objects.record_failure(_FAULT_KEY, "SlackUnavailable", broken)
        return {
            "mode": "fallback",
            "reason": broken,
            "posted": 0,
            "nudged": 0,
            "remaining": DeferredQuestion.owner_pending().count(),
            "cards": _linked_cards(),
        }
    ScannerFault.objects.heal(_FAULT_KEY)
    user_id = getattr(backend, "user_id", "") if backend is not None else ""
    posted, _candidates = drain_unmirrored_deferred_questions(user_id=user_id, overlay=overlay, backend=backend)
    nudged, _candidates = reask_escalated_questions(user_id=user_id, overlay=overlay, backend=backend)
    broken = _broken_reason(backend, listener_stale=listener_stale)
    if broken:
        ScannerFault.objects.record_failure(_FAULT_KEY, "SlackUnavailable", broken)
    return {
        "mode": "fallback" if broken else "slack",
        "reason": broken,
        "posted": posted,
        "nudged": nudged,
        "remaining": DeferredQuestion.unmirrored_pending().count(),
        "cards": _linked_cards(),
    }
