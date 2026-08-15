"""A review that completed without ever running must be visible here (#4308).

``completed`` + ``exit_code = 0`` is the success signal every consumer reads, and a reviewing
row carrying ZERO :class:`~teatree.core.models.task_attempt.TaskAttempt` rows satisfies it
having done nothing at all — so a PR whose only verdict was a stale HOLD read as reviewed
three times while it stayed held. The producers of that shape now record an attempt naming
the skip; this is the surface that catches whichever one does not, without an operator
hand-querying the control DB to find it.

Bounded to :data:`_WINDOW_DAYS` because the finding is actionable only while the PR is still
live — an unbounded scan would report the same historic rows every run until nobody reads
the output.
"""

from datetime import timedelta

import typer
from django.utils import timezone

from teatree.core.modelkit.phase_tools import VERDICT_REVIEW_PHASES
from teatree.utils.url_slug import pr_ref_from_url

#: How far back a zero-attempt review is still worth acting on.
_WINDOW_DAYS = 14


def _reviewed_ref(issue_url: str) -> str:
    """``<slug>#<n>`` for the PR under review, else the raw url — never a bare id."""
    ref = pr_ref_from_url(issue_url)
    return f"{ref.slug}#{ref.pr_id}" if ref is not None else issue_url


def check_reviewing_ledger() -> bool:
    """FAIL on every recently-completed review phase task that recorded no attempt."""
    from teatree.core.models.task import Task  # noqa: PLC0415 — deferred: ORM import needs the app registry

    try:
        empty = list(
            Task.objects.filter(
                status=Task.Status.COMPLETED,
                phase__in=sorted(VERDICT_REVIEW_PHASES),
                attempts__isnull=True,
                created_at__gte=timezone.now() - timedelta(days=_WINDOW_DAYS),
            ).select_related("ticket")
        )
    except Exception as exc:  # noqa: BLE001 — doctor check must never crash the run
        typer.echo(
            f"WARN  Reviewing ledger UNVERIFIED: the task ledger could not be read ({exc.__class__.__name__}: {exc})."
        )
        return True
    for task in empty:
        url = task.ticket.issue_url
        typer.echo(
            f"FAIL  Task {task.pk} ({task.phase}) on {_reviewed_ref(url)} completed with no attempt recorded — "
            f"nothing ran, yet the row reads as a finished review, so a stale or missing verdict keeps binding. "
            f"Check what the PR actually has: t3 <overlay> review status {url}, and re-arm a review if it holds none."
        )
    return not empty


__all__ = ["check_reviewing_ledger"]
