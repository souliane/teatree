"""Explicit Slack delivery for owner questions during a check-in."""

from datetime import datetime, timedelta
from typing import TypedDict

from django.db.models import Exists, OuterRef
from django.utils import timezone

from teatree.core.backend_protocols import MessagingBackend
from teatree.core.checking_report.status import plain_title
from teatree.core.models import BotPing, DeferredQuestion, PendingChatInjection
from teatree.core.models.scanner_fault import ScannerFault
from teatree.core.notify import resolve_owner_dm_backend
from teatree.core.notify_question_drains import drain_unmirrored_deferred_questions, reask_escalated_questions
from teatree.core.question_heal import live_owner_questions
from teatree.utils.slack_links import slack_permalink

_FAULT_KEY = "checking:slack"


def _unprocessed_owner_answer() -> bool:
    pending_reply = PendingChatInjection.loop_unreplied().filter(
        channel=OuterRef("slack_channel"),
        thread_ts=OuterRef("slack_ts"),
        received_at__lt=timezone.now() - timedelta(minutes=2),
    )
    return DeferredQuestion.owner_pending().exclude(slack_ts="").filter(Exists(pending_reply)).exists()


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


def _broken_reason(
    backend: MessagingBackend | None, *, listener_stale: bool, failed_since: datetime | None = None
) -> str:
    if backend is None:
        return "Slack post path is unavailable"
    if listener_stale:
        return "Slack answer listener is stale"
    if _unprocessed_owner_answer():
        return "Slack owner answers are not being processed"
    if failed_since is None:
        return ""
    latest = (
        BotPing.objects.filter(kind=BotPing.Kind.QUESTION, posted_at__gte=failed_since)
        .order_by("-posted_at", "-pk")
        .first()
    )
    if latest and latest.status in {BotPing.Status.FAILED, BotPing.Status.NOOP}:
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
    live_owner_questions()
    backend, _reason = resolve_owner_dm_backend()
    broken = _broken_reason(backend, listener_stale=listener_stale)
    if broken:
        ScannerFault.objects.record_failure(_FAULT_KEY, "SlackUnavailable", broken)
        return {
            "mode": "fallback",
            "reason": broken,
            "posted": 0,
            "nudged": 0,
            "remaining": DeferredQuestion.unmirrored_pending().count(),
            "cards": _linked_cards(),
        }
    ScannerFault.objects.heal(_FAULT_KEY)
    user_id = getattr(backend, "user_id", "") if backend is not None else ""
    started = timezone.now()
    posted, candidates = drain_unmirrored_deferred_questions(user_id=user_id, overlay=overlay, backend=backend)
    nudged, _candidates = reask_escalated_questions(user_id=user_id, overlay=overlay, backend=backend)
    broken = _broken_reason(backend, listener_stale=listener_stale, failed_since=started)
    if broken:
        ScannerFault.objects.record_failure(_FAULT_KEY, "SlackUnavailable", broken)
    remaining = DeferredQuestion.unmirrored_pending().count()
    return {
        "mode": "fallback" if broken else "slack",
        "reason": broken
        or ("Slack send paused until the next pass" if candidates and not posted and remaining else ""),
        "posted": posted,
        "nudged": nudged,
        "remaining": remaining,
        "cards": _linked_cards(),
    }
