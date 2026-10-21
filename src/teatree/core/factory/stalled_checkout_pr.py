"""Health signal for old pull requests with checkout-parked tasks."""

from datetime import timedelta

from django.db.models import Exists, F, OuterRef, Q, Subquery
from django.utils import timezone

from teatree.core.factory.health_signal import HealthSignal, SignalCollection
from teatree.core.models import PullRequest, Task, TaskAttempt
from teatree.core.models.known_issue import KnownIssue
from teatree.core.models.usage_window_state import CHECKOUT_PARKED_PREFIX
from teatree.core.ref_render import render_ref

STALLED_PR_AGE = timedelta(hours=24)


def stalled_checkout_pr_signals() -> SignalCollection:
    now = timezone.now()
    newest_error = TaskAttempt.objects.filter(task_id=OuterRef("pk")).order_by("-pk").values("error")[:1]
    live_claims = Task.objects.filter(
        ticket_id=OuterRef("ticket_id"), status=Task.Status.CLAIMED, lease_expires_at__gt=now
    )
    parked = (
        Task.objects.filter(status=Task.Status.PENDING)
        .annotate(newest_error=Subquery(newest_error))
        .filter(newest_error__startswith=CHECKOUT_PARKED_PREFIX)
        .exclude(Exists(live_claims))
    )
    stalled = (
        PullRequest.objects.live()
        .filter(Q(create_verified_at__lte=now - STALLED_PR_AGE) | Q(create_verified_at__isnull=True))
        .filter(ticket_id__in=parked.values("ticket_id"))
        .select_related("ticket")
        .order_by("ticket_id", F("create_verified_at").asc(nulls_first=True), "pk")
    )
    signals: dict[int, HealthSignal] = {}
    for pr in stalled:
        if pr.ticket_id in signals:
            continue
        ticket = render_ref(f"#{pr.ticket_id}", title=pr.ticket.short_description)
        signals[pr.ticket_id] = HealthSignal(
            fingerprint=f"stalled-checkout-pr:{pr.ticket_id}",
            severity=KnownIssue.Severity.WARNING,
            kind="stalled_checkout_pr",
            overlay=pr.overlay,
            summary=(
                f"ticket {ticket} has a pull request open over {int(STALLED_PR_AGE.total_seconds() // 3600)}h, "
                "with a checkout-parked task and no live claim"
            ),
            evidence_url=pr.url,
        )
    return SignalCollection(tuple(signals.values()))
