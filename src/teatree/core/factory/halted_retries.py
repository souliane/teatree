"""Live health signals for failed tasks whose automatic retries stopped."""

import hashlib

from django.db.models import Max

from teatree.core.factory.health_signal import HealthSignal, SignalCollection
from teatree.core.factory.stuck_ticket_halt import ticket_halt_is_current
from teatree.core.modelkit.task_failure_taxonomy import HALT_STAMP, FailureKind
from teatree.core.models import Task, Ticket
from teatree.core.models.known_issue import KnownIssue
from teatree.core.ref_render import render_ref


def halted_retry_signals() -> SignalCollection:
    halted = (
        Task.objects.filter(status=Task.Status.FAILED, execution_reason__contains=HALT_STAMP)
        .exclude(
            ticket__state__in=(
                Ticket.State.PR_OPENED,
                Ticket.State.MERGED,
                Ticket.State.DELIVERED,
                Ticket.State.REVIEW_DELIVERED,
                Ticket.State.IGNORED,
            )
        )
        .exclude(failure_kind=FailureKind.CANCELLED)
        .select_related("ticket")
        .prefetch_related("attempts")
    )
    completed = {
        (row["ticket_id"], row["phase"]): row["latest"]
        for row in Task.objects.filter(status=Task.Status.COMPLETED)
        .values("ticket_id", "phase")
        .annotate(latest=Max("pk"))
    }
    signals: dict[tuple[int, str, str], HealthSignal] = {}
    for task in halted:
        if completed.get((task.ticket_id, task.phase), 0) > task.pk:
            continue
        attempts = list(task.attempts.all())
        latest = max(attempts, key=lambda row: row.pk) if attempts else None
        cause = latest.error if latest is not None and latest.error else task.failure_reason or "no recorded error"
        ticket = render_ref(f"#{task.ticket_id}", title=task.ticket.short_description)
        fingerprint = (
            latest.error_fingerprint
            if latest is not None and latest.error_fingerprint
            else hashlib.sha256(cause.encode()).hexdigest()
        )
        signals[task.ticket_id, task.phase, fingerprint] = HealthSignal(
            fingerprint=f"halted-retries:{task.ticket_id}:{task.phase}:{fingerprint}",
            severity=KnownIssue.Severity.WARNING,
            kind="halted_retries",
            overlay=task.ticket.overlay,
            summary=f"ticket {ticket} stopped retrying {task.phase}: {cause}",
            evidence_url=task.ticket.issue_url,
        )
    return SignalCollection(tuple(signals.values()))


def stuck_ticket_halt_signals() -> SignalCollection:
    tickets = Ticket.objects.filter(extra__stuck_halt__isnull=False).exclude(
        state__in=(
            Ticket.State.PR_OPENED,
            Ticket.State.MERGED,
            Ticket.State.DELIVERED,
            Ticket.State.REVIEW_DELIVERED,
            Ticket.State.IGNORED,
        )
    )
    signals = []
    for ticket in tickets:
        if not ticket_halt_is_current(ticket):
            continue
        stamp = ticket.extra["stuck_halt"]
        reason = str(stamp["reason"])
        signals.append(
            HealthSignal(
                fingerprint=f"stuck-halt:{ticket.pk}:{stamp['newest_task']}:{stamp['reviewed_sha']}",
                severity=KnownIssue.Severity.WARNING,
                kind="halted_retries",
                overlay=ticket.overlay,
                summary=(
                    f"ticket {render_ref(f'#{ticket.pk}', title=ticket.short_description)} "
                    f"stopped redispatching: {reason}"
                ),
                evidence_url=ticket.issue_url,
            )
        )
    return SignalCollection(tuple(signals))
