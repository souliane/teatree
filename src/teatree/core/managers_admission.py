"""Seat predicates shared by task admission probes and conditional writes.

An admitted pending task keeps its seat through the runner handoff for five minutes, while it is due.
After that, or once it is parked into the future, the seat is released so it cannot hold the lane closed.
"""

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, cast

from django.apps import apps
from django.db import models
from django.db.models import Q
from django.db.models.functions import Cast, Coalesce, Concat
from django.db.models.lookups import LessThan

from teatree.core.managers_task_claim import _claimable_now_q
from teatree.core.modelkit.phases import ANSWERING_PHASES, CheapClass, PhaseCost, cheap_phase_spellings

if TYPE_CHECKING:
    from teatree.core.models.task import Task
    from teatree.core.models.task_attempt import TaskAttempt
    from teatree.core.models.worktree import Worktree


ADMITTED_INFLIGHT_WINDOW = timedelta(minutes=5)


def _cheap_phase_q(cheap_class: CheapClass | None = None) -> Q:
    """Every row of the cheap phase class, whichever spelling it was stored with.

    The lane's membership test, held apart from any one question about it so the seat
    predicates below share a single hop to the phase vocabulary.
    """
    return Q(phase__in=cheap_class.spellings() if cheap_class is not None else cheap_phase_spellings())


def _answering_phase_q() -> Q:
    """Every row of the owner-answering class (its phases carry no short-verb aliases)."""
    return Q(phase__in=ANSWERING_PHASES)


def _unseated_q(now: datetime) -> Q:
    """A row holding no live seat: never admitted, or its seat lapsed with the window."""
    return Q(admitted_at__isnull=True) | Q(admitted_at__lte=now - ADMITTED_INFLIGHT_WINDOW)


def _lane_occupancy_q(now: datetime, *, lane: PhaseCost | CheapClass) -> Q:
    """The rows holding a seat in one cost class's lane at *now*.

    One predicate rather than two: :meth:`TaskQuerySet.cheap_class_occupancy` reads it as a
    ``COUNT`` and :meth:`TaskQuerySet.record_admission` embeds it in the ``WHERE`` of the
    conditional stamp, and a bound whose probe and whose arbitration disagreed would be no
    bound at all. The cheap classes and the answering class partition work from the expensive class, which is
    their exact complement; an unregistered phase therefore lands in the braked class,
    matching :func:`~teatree.core.modelkit.phases.phase_cost`'s own fail-safe (#4374).
    The answering class sits outside the cheap lane's total, so it never takes a review or retiring seat.
    ``PhaseCost.CHEAP`` is the one definition of the whole cheap lane (both classes) that the
    total-occupancy read and the conditional write's total bound share.
    """
    task_model = cast("type[Task]", apps.get_model("core", "Task"))

    if lane is PhaseCost.CHEAP:
        membership = _cheap_phase_q()
    elif lane is PhaseCost.ANSWERING:
        membership = _answering_phase_q()
    elif lane is PhaseCost.EXPENSIVE:
        membership = ~(_cheap_phase_q() | _answering_phase_q())
    else:
        membership = _cheap_phase_q(lane)
    return membership & (
        Q(status=task_model.Status.CLAIMED, lease_expires_at__gt=now)
        | (Q(status=task_model.Status.PENDING, admitted_at__gt=now - ADMITTED_INFLIGHT_WINDOW) & _claimable_now_q(now))
    )


def _lane_under_ceiling(now: datetime, ceiling: int, *, lane: PhaseCost | CheapClass) -> LessThan:
    """A ``WHERE`` term true only while that lane has a free seat at *now*.

    ``Coalesce`` is load-bearing: the grouped ``COUNT`` yields NO row for an empty lane, and
    ``NULL < ceiling`` is not true — an empty lane would refuse every admission.
    """
    task_model = cast("type[Task]", apps.get_model("core", "Task"))

    occupied = (
        task_model.objects.filter(_lane_occupancy_q(now, lane=lane))
        .order_by()
        .values(_lane=models.Value(1))
        .annotate(seats=models.Count("*"))
        .values("seats")[:1]
    )
    return LessThan(
        Coalesce(models.Subquery(occupied, output_field=models.IntegerField()), models.Value(0)),
        ceiling,
    )


def seated_pending_rows(queryset: models.QuerySet, now: datetime) -> models.QuerySet:
    """PENDING rows still inside their admission window — the complement of :func:`_unseated_q`."""
    task_model = cast("type[Task]", apps.get_model("core", "Task"))
    return queryset.filter(status=task_model.Status.PENDING).exclude(_unseated_q(now))


def waiting_for_admission_rows(queryset: models.QuerySet, now: datetime) -> models.QuerySet:
    """Due, unseated PENDING rows whose ticket has no held checkout requiring a live holder."""
    task_model = cast("type[Task]", apps.get_model("core", "Task"))
    worktree_model = cast("type[Worktree]", apps.get_model("core", "Worktree"))
    finished_unowned_holders = (
        task_model.objects.filter(
            ticket_id=models.OuterRef("ticket_id"),
            status__in=task_model.Status.terminal(),
            owner_pid__isnull=True,
        )
        .annotate(holder=Concat(models.Value("task:"), Cast("pk", models.CharField())))
        .values("holder")
    )
    held = (
        worktree_model.objects.filter(ticket_id=models.OuterRef("ticket_id"), occupancy_expires_at__gt=now)
        .exclude(occupied_by="")
        .exclude(occupied_by__in=models.Subquery(finished_unowned_holders))
    )
    return (
        queryset.filter(_claimable_now_q(now), _unseated_q(now), status=task_model.Status.PENDING)
        .exclude(models.Exists(held))
        .select_related("ticket")
    )


def rearm_parked_behind_rows(queryset: models.QuerySet, now: datetime, *, holder: str, holder_session: str) -> int:
    """Make due now, seatless, each parked row whose newest attempt was refused by ``(holder, holder_session)``."""
    task_model = cast("type[Task]", apps.get_model("core", "Task"))
    attempt_model = cast("type[TaskAttempt]", apps.get_model("core", "TaskAttempt"))
    parked = queryset.filter(status=task_model.Status.PENDING, not_before__gt=now)
    newest = attempt_model.objects.filter(task=models.OuterRef("pk")).order_by("-pk").values("pk")[:1]
    behind = attempt_model.objects.filter(
        pk__in=parked.annotate(newest_attempt=models.Subquery(newest)).values("newest_attempt"),
        result__occupied_by__holder=holder,
        result__occupied_by__session=holder_session,
    )
    return parked.filter(pk__in=behind.values("task_id")).update(not_before=now, admitted_at=None)
