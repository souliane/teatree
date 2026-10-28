"""Standing health signal for a checkout-park episode that has not cleared."""

from datetime import timedelta

from django.db.models import Exists, OuterRef, Subquery
from django.utils import timezone

from teatree.core.factory.health_signal import HealthSignal, SignalCollection
from teatree.core.models import Task, TaskAttempt
from teatree.core.models.known_issue import KnownIssue
from teatree.core.models.usage_window_state import CHECKOUT_PARKED_PREFIX
from teatree.core.ref_render import render_ref

STALLED_CHECKOUT_AGE = timedelta(hours=1)


def _episode_start(attempt: TaskAttempt, starts: dict[int, TaskAttempt]) -> TaskAttempt:
    result = attempt.result if isinstance(attempt.result, dict) else {}
    marker = result.get("checkout_episode", "")
    if isinstance(marker, str) and marker.startswith("attempt:"):
        pk = marker.removeprefix("attempt:")
        if pk.isdecimal():
            start = starts.get(int(pk))
            if start is not None and start.task.ticket_id == attempt.task.ticket_id:
                return start
    return attempt


def stalled_checkout_signals() -> SignalCollection:
    now = timezone.now()
    newest = TaskAttempt.objects.filter(task_id=OuterRef("pk")).order_by("-pk")
    live_claims = Task.objects.filter(
        ticket_id=OuterRef("ticket_id"), status=Task.Status.CLAIMED, lease_expires_at__gt=now
    )
    parked = (
        Task.objects.filter(status=Task.Status.PENDING)
        .annotate(newest_error=Subquery(newest.values("error")[:1]), newest_pk=Subquery(newest.values("pk")[:1]))
        .filter(newest_error__startswith=CHECKOUT_PARKED_PREFIX)
        .exclude(Exists(live_claims))
        .select_related("ticket")
    )
    rows = list(parked)
    latest = {
        attempt.pk: attempt
        for attempt in TaskAttempt.objects.filter(pk__in=[row.newest_pk for row in rows]).select_related("task__ticket")
    }
    episode_ids = {
        int(marker.removeprefix("attempt:"))
        for attempt in latest.values()
        if isinstance(attempt.result, dict)
        if isinstance(marker := attempt.result.get("checkout_episode"), str)
        if marker.startswith("attempt:") and marker.removeprefix("attempt:").isdecimal()
    }
    starts = {
        attempt.pk: attempt
        for attempt in TaskAttempt.objects.filter(
            pk__in=episode_ids, error__startswith=CHECKOUT_PARKED_PREFIX
        ).select_related("task__ticket")
    }
    signals: dict[int, HealthSignal] = {}
    for task in rows:
        attempt = latest.get(task.newest_pk)
        if attempt is None:
            continue
        start = _episode_start(attempt, starts)
        if (start.ended_at or start.started_at) > now - STALLED_CHECKOUT_AGE:
            continue
        ticket = render_ref(f"#{task.ticket_id}", title=task.ticket.short_description)
        signals[task.ticket_id] = HealthSignal(
            fingerprint=f"stalled-checkout:{task.ticket_id}",
            severity=KnownIssue.Severity.WARNING,
            kind="stalled_checkout",
            overlay=task.ticket.overlay,
            summary=(
                f"ticket {ticket} has waited over one hour for a checkout: "
                f"{attempt.error.removeprefix(CHECKOUT_PARKED_PREFIX)}"
            ),
            evidence_url=task.ticket.issue_url,
        )
    return SignalCollection(tuple(signals.values()))
