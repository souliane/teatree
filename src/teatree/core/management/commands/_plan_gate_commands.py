"""Plan-gate operator command logic, factored out of ``ticket.py``.

The ``t3 <overlay> ticket`` group exposes four plan-gate operator commands —
``plan`` (record a real plan), ``plan-bypass`` (audited ``--human-authorize``
bypass), ``skip-planning`` (lightweight trivial-work carve-out), and
``plan-reconcile-inflight`` (retroactive one-time advance). They share one shape:
validate input, then in a single ``transaction.atomic`` block produce a
satisfying signal (a ``PlanArtifact`` or a ``trivial_plan_skip`` marker), drive
``ticket.plan()`` STARTED → PLANNED, and ``save()``.

This module owns that shared shape so the command methods in ``ticket.py`` stay
thin delegators (one cohesive concern, one home — and ``ticket.py`` stays under
its module-health LOC cap). The command methods keep the django-typer
``@command`` decoration + CLI signature; the work lives here.
"""

from typing import TYPE_CHECKING, TypedDict

from django.db import transaction
from django_fsm import TransitionNotAllowed

from teatree.core.models import Ticket
from teatree.core.models.errors import InvalidTransitionError

if TYPE_CHECKING:
    from teatree.core.models.plan_artifact import PlanArtifact


class PlanResult(TypedDict, total=False):
    ticket_id: int
    artifact_id: int
    state: str
    error: str


class PlanReconcileResult(TypedDict, total=False):
    inspected: int
    bypassed: int
    skipped: int


class PlanAdvanceError(Exception):
    """The plan() advance was refused; ``message`` is the surfaced reason."""

    def __init__(self, ticket: Ticket, message: str) -> None:
        super().__init__(message)
        self.ticket = ticket
        self.message = message


def record_artifact_and_advance(*, ticket: Ticket, plan_text: str, recorded_by: str) -> "PlanArtifact":
    """Record a PlanArtifact and drive ``ticket.plan()`` in one atomic block.

    Raises :class:`PlanAdvanceError` (carrying the ticket + surfaced reason) when
    the artifact factory rejects the input or the FSM refuses the transition, so
    a failed advance rolls back the artifact write and the caller can return a
    structured error.
    """
    from teatree.core.models.plan_artifact import PlanArtifact  # noqa: PLC0415

    try:
        with transaction.atomic():
            artifact = PlanArtifact.record(ticket=ticket, plan_text=plan_text, recorded_by=recorded_by)
            ticket.plan()
            ticket.save()
    except (ValueError, TransitionNotAllowed, InvalidTransitionError) as exc:
        raise PlanAdvanceError(ticket, str(exc)) from exc
    return artifact


def record_trivial_skip_and_advance(*, ticket: Ticket, reason: str, by: str) -> None:
    """Record a trivial-skip marker and drive ``ticket.plan()`` in one atomic block.

    The lightweight sibling of :func:`record_artifact_and_advance` — no
    ``PlanArtifact`` is written; the marker is the satisfying signal. Raises
    :class:`PlanAdvanceError` on a rejected marker or a refused transition (the
    atomic block rolls the marker write back).
    """
    from teatree.core.models.trivial_plan_skip import mark_trivial_plan_skip  # noqa: PLC0415

    try:
        with transaction.atomic():
            mark_trivial_plan_skip(ticket, reason=reason, by=by)
            ticket.plan()
            ticket.save()
    except (ValueError, TransitionNotAllowed, InvalidTransitionError) as exc:
        raise PlanAdvanceError(ticket, str(exc)) from exc


def reconcile_inflight(*, authorizer: str, issue_ref: str, dry_run: bool) -> tuple[PlanReconcileResult, list[str]]:
    """Retroactively advance every STARTED ticket to PLANNED via an audited bypass.

    Returns the tally plus a list of human-readable log lines for the caller to
    emit. A per-ticket transition refusal is recorded as skipped, never raised,
    so one stuck ticket does not abort the sweep.
    """
    from teatree.core.models.plan_artifact import PlanArtifact  # noqa: PLC0415

    started = list(Ticket.objects.filter(state=Ticket.State.STARTED))
    log: list[str] = [f"  found {len(started)} STARTED ticket(s)"]
    bypassed = 0
    skipped = 0
    for ticket in started:
        reason = "retroactive — PLANNED state added mid-flight" + (f" ({issue_ref})" if issue_ref else "")
        if dry_run:
            log.append(f"  [dry-run] would bypass ticket {ticket.pk}: {reason}")
            skipped += 1
            continue
        try:
            with transaction.atomic():
                PlanArtifact.record(
                    ticket=ticket,
                    plan_text=f"[audited bypass by {authorizer}] {reason}",
                    recorded_by=authorizer,
                )
                ticket.plan()
                ticket.save()
            log.append(f"  ticket {ticket.pk}: STARTED → PLANNED (bypass recorded)")
            bypassed += 1
        except (ValueError, TransitionNotAllowed, InvalidTransitionError) as exc:
            log.append(f"  ticket {ticket.pk}: skipped — {exc}")
            skipped += 1
    return PlanReconcileResult(inspected=len(started), bypassed=bypassed, skipped=skipped), log
