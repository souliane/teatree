"""Health signal for old pull requests whose next task waits at the checkout fence."""

from datetime import timedelta
from textwrap import shorten

from django.db.models import F, OuterRef, Q, Subquery
from django.utils import timezone

from teatree.core.factory.health_signal import HealthSignal, SignalCollection
from teatree.core.models import PullRequest, Task, TaskAttempt
from teatree.core.models.known_issue import KnownIssue
from teatree.core.models.usage_window_state import CHECKOUT_PARKED_PREFIX
from teatree.core.ref_render import render_ref

STALLED_PR_AGE = timedelta(hours=24)
_REASON_WIDTH = 200


def stalled_checkout_pr_signals() -> SignalCollection:
    now = timezone.now()
    newest = TaskAttempt.objects.filter(task=OuterRef("pk")).order_by("-pk")
    parked = (
        Task.objects.filter(status=Task.Status.PENDING)
        .annotate(newest_attempt=Subquery(newest.values("pk")[:1]), newest_error=Subquery(newest.values("error")[:1]))
        .filter(newest_error__startswith=CHECKOUT_PARKED_PREFIX)
    )
    reasons = dict(parked.order_by("newest_attempt").values_list("ticket_id", "newest_error"))
    stalled = (
        PullRequest.objects.live()
        .filter(Q(create_verified_at__lte=now - STALLED_PR_AGE) | Q(create_verified_at__isnull=True))
        .filter(ticket_id__in=reasons)
        .exclude(ticket_id__in=Task.objects.active_claims().values("ticket_id"))
        .select_related("ticket")
        .order_by("ticket_id", F("create_verified_at").asc(nulls_first=True), "pk")
    )
    signals: dict[int, HealthSignal] = {}
    for pr in stalled:
        if pr.ticket_id in signals:
            continue
        ticket = render_ref(f"#{pr.ticket_id}", title=pr.ticket.short_description)
        reason = shorten(reasons[pr.ticket_id].removeprefix(CHECKOUT_PARKED_PREFIX), _REASON_WIDTH, placeholder="…")
        signals[pr.ticket_id] = HealthSignal(
            fingerprint=f"stalled-checkout-pr:{pr.ticket_id}",
            severity=KnownIssue.Severity.WARNING,
            kind="stalled_checkout_pr",
            overlay=pr.overlay,
            summary=(
                f"ticket {ticket} has a pull request open over {int(STALLED_PR_AGE.total_seconds() // 3600)}h, "
                f"nothing running and its next task parked at the checkout fence: {reason}"
            ),
            evidence_url=pr.url,
        )
    return SignalCollection(tuple(signals.values()))
