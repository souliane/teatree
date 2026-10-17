"""Seat predicates shared by task admission probes and conditional writes.

An admitted pending task keeps its seat through the runner handoff for five minutes, while it is due.
After that, or once it is parked into the future, the seat is released so it cannot hold the lane closed.
"""

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, cast

from django.apps import apps
from django.db import models
from django.db.models import Q
from django.db.models.functions import Coalesce
from django.db.models.lookups import LessThan

from teatree.core.managers_task_claim import _claimable_now_q
from teatree.core.modelkit.phases import cheap_phase_spellings

if TYPE_CHECKING:
    from teatree.core.models.task import Task
    from teatree.core.models.task_attempt import TaskAttempt
    from teatree.core.models.worktree import Worktree


ADMITTED_INFLIGHT_WINDOW = timedelta(minutes=5)


def _cheap_phase_q() -> Q:
    """Every row of the cheap phase class, whichever spelling it was stored with.

    The lane's membership test, held apart from any one question about it so the seat
    predicates below share a single hop to the phase vocabulary.
    """
    return Q(phase__in=cheap_phase_spellings())


def _unseated_q(now: datetime) -> Q:
    """A row holding no live seat: never admitted, or its seat lapsed with the window."""
    return Q(admitted_at__isnull=True) | Q(admitted_at__lte=now - ADMITTED_INFLIGHT_WINDOW)


def _lane_occupancy_q(now: datetime, *, cheap: bool) -> Q:
    """The rows holding a seat in one cost class's lane at *now*.

    One predicate rather than two: :meth:`TaskQuerySet.cheap_lane_occupancy` reads it as a
    ``COUNT`` and :meth:`TaskQuerySet.record_admission` embeds it in the ``WHERE`` of the
    conditional stamp, and a bound whose probe and whose arbitration disagreed would be no
    bound at all. The two lanes PARTITION the queue —
    the expensive one is the exact complement of the cheap one — so an unregistered
    phase lands in the braked class,
    matching :func:`~teatree.core.modelkit.phases.phase_cost`'s own fail-safe (#4374).
    """
    task_model = cast("type[Task]", apps.get_model("core", "Task"))

    membership = _cheap_phase_q() if cheap else ~_cheap_phase_q()
    return membership & (
        Q(status=task_model.Status.CLAIMED, lease_expires_at__gt=now)
        | (Q(status=task_model.Status.PENDING, admitted_at__gt=now - ADMITTED_INFLIGHT_WINDOW) & _claimable_now_q(now))
    )


def _lane_under_ceiling(now: datetime, ceiling: int, *, cheap: bool) -> LessThan:
    """A ``WHERE`` term true only while that lane has a free seat at *now*.

    ``Coalesce`` is load-bearing: the grouped ``COUNT`` yields NO row for an empty lane, and
    ``NULL < ceiling`` is not true — an empty lane would refuse every admission.
    """
    task_model = cast("type[Task]", apps.get_model("core", "Task"))

    occupied = (
        task_model.objects.filter(_lane_occupancy_q(now, cheap=cheap))
        .order_by()
        .values(_lane=models.Value(1))
        .annotate(seats=models.Count("*"))
        .values("seats")[:1]
    )
    return LessThan(
        Coalesce(models.Subquery(occupied, output_field=models.IntegerField()), models.Value(0)),
        ceiling,
    )


def waiting_for_admission_rows(queryset: models.QuerySet, now: datetime) -> models.QuerySet:
    """Due, unseated PENDING rows whose ticket's checkout no live occupant holds, as ``acquire`` would refuse."""
    task_model = cast("type[Task]", apps.get_model("core", "Task"))
    worktree_model = cast("type[Worktree]", apps.get_model("core", "Worktree"))
    held = worktree_model.objects.filter(ticket_id=models.OuterRef("ticket_id"), occupancy_expires_at__gt=now).exclude(
        occupied_by=""
    )
    return (
        queryset.filter(_claimable_now_q(now), _unseated_q(now), status=task_model.Status.PENDING)
        .exclude(models.Exists(held))
        .select_related("ticket")
    )


def wake_parked_behind_rows(queryset: models.QuerySet, now: datetime, *, holder: str, holder_session: str) -> int:
    """Make due now, seatless, each parked row whose newest attempt was refused by ``(holder, holder_session)``."""
    task_model = cast("type[Task]", apps.get_model("core", "Task"))
    attempt_model = cast("type[TaskAttempt]", apps.get_model("core", "TaskAttempt"))
    parked = queryset.filter(status=task_model.Status.PENDING, not_before__gt=now)
    newest = attempt_model.objects.filter(task=models.OuterRef("pk")).order_by("-pk").values("pk")[:1]
    behind = attempt_model.objects.filter(
        pk__in=parked.annotate(newest_attempt=models.Subquery(newest)).values("newest_attempt"),
        result__checkout_blocked_by__holder=holder,
        result__checkout_blocked_by__session=holder_session,
    )
    return parked.filter(pk__in=behind.values("task_id")).update(not_before=now, admitted_at=None)
