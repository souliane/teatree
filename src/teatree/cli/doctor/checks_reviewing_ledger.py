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

#: Pull requests named inline before the finding switches to a count. One finding per
#: affected ROW turns a single incident into hundreds of lines: the operator surface that
#: consumes this batches red findings into notifications, so a backlog of historic rows
#: arrives as dozens of messages describing ONE condition. Volume is not incident count —
#: the aggregate says how bad it is, and the listed command enumerates it on demand.
_REFS_SHOWN = 8


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
    if not empty:
        return True
    refs = sorted({_reviewed_ref(task.ticket.issue_url) for task in empty})
    shown = ", ".join(refs[:_REFS_SHOWN])
    more = f", and {len(refs) - _REFS_SHOWN} more" if len(refs) > _REFS_SHOWN else ""
    typer.echo(
        f"FAIL  {len(empty)} completed review task(s) across {len(refs)} pull request(s) recorded no attempt — "
        f"nothing ran, yet each row reads as a finished review, so a stale or missing verdict keeps binding. "
        f"Affected: {shown}{more}. "
        f"Enumerate them with "
        f"`t3 <overlay> tasks list --status completed --json | jq '.[] | select(.phase == \"reviewing\")'`, "
        f"then check each PR with `t3 <overlay> review status <pr-url>` and re-arm a review where it holds none."
    )
    return False


__all__ = ["check_reviewing_ledger"]
