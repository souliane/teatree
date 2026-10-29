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
from typing import TYPE_CHECKING

import typer
from django.utils import timezone

from teatree.core.modelkit.phase_tools import VERDICT_REVIEW_PHASES
from teatree.utils.run import redact_secrets
from teatree.utils.url_slug import pr_ref_from_url

if TYPE_CHECKING:
    from teatree.core.models import Task


def _review_saturation_reason(task: "Task | None") -> str:
    if task is None:
        return "no linked review task for this head"
    attempt = task.attempts.exclude(error="").order_by("-pk").first()
    error = (attempt.error if attempt is not None else task.failure_reason).strip()
    if not error:
        latest = task.attempts.order_by("-pk").first()
        if latest is None:
            reason = "no recorded attempt"
        else:
            result = latest.result if isinstance(latest.result, dict) else {}
            review = result.get("review_verdict")
            if isinstance(review, dict) and str(review.get("verdict", "")).strip().lower() == "unverified":
                gaps = review.get("evidence_gaps")
                reason = (
                    "unverified verdict: " + ", ".join(str(gap) for gap in gaps)
                    if isinstance(gaps, list)
                    else "unverified verdict"
                )
            else:
                reason = "attempt recorded without verdict or failure reason"
        return reason
    lowered = error.casefold()
    if "turn ceiling" in lowered or "maximum turns" in lowered:
        return "turn ceiling"
    if "brief refused" in lowered or "brief could not" in lowered:
        return "refused brief"
    if "credential" in lowered or "api_credit:" in lowered:
        return "credential"
    lines = [line.strip() for line in error.splitlines() if line.strip()]
    cause = lines[-1] if lines[0].startswith("Traceback (most recent call last):") else lines[0]
    return redact_secrets(cause)[:160]


#: How far back a zero-attempt review is still worth acting on.
_WINDOW_DAYS = 14

#: Ticket states in which a missing verdict can no longer be acted on. The remedy this check
#: prescribes is "re-arm a review", and once the pull request has landed there is nothing left
#: to re-arm — the verdict binds no tree and gates no merge. Age alone did not express that:
#: the window bounds how OLD a row may be, not whether its work is still open, so a fortnight
#: of landed reviews reported as live findings every run. Read from the local ticket rather
#: than the forge, because a doctor check must stay a fast local read.
_SPENT_TICKET_STATE_NAMES = ("MERGED", "DELIVERED", "IGNORED")


def _spent_ticket_states() -> set[str]:
    """The spent states, imported inside the call like this module's other ORM import.

    A module-level ORM import crashes every venue that loads the doctor without Django set
    up — the pre-push hook among them, which died on `ImproperlyConfigured: Requested
    setting INSTALLED_APPS`. Naming the members rather than hardcoding their values keeps
    the binding: a renamed member raises here instead of silently matching nothing.
    """
    from teatree.core.models.ticket import Ticket  # noqa: PLC0415 — deferred: ORM import needs the app registry

    return {getattr(Ticket.State, name) for name in _SPENT_TICKET_STATE_NAMES}


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
            )
            .exclude(ticket__state__in=sorted(_spent_ticket_states()))
            .select_related("ticket")
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
