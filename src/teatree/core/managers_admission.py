"""Seat predicates shared by task admission probes and conditional writes.

An admitted pending task keeps its seat through the runner handoff for five minutes.
After that, the seat is released so a dead runner cannot hold the lane closed.
"""

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, cast

from django.apps import apps
from django.db import models
from django.db.models import Q
from django.db.models.functions import Coalesce
from django.db.models.lookups import LessThan

from teatree.core.modelkit.phases import cheap_phase_spellings

if TYPE_CHECKING:
    from teatree.core.models.task import Task


ADMITTED_INFLIGHT_WINDOW = timedelta(minutes=5)


def _cheap_phase_q() -> Q:
    """Every row of the cheap phase class, whichever spelling it was stored with.

    The lane's membership test, held apart from any one question about it so the seat
    predicates below share a single hop to the phase vocabulary.
    """
    return Q(phase__in=cheap_phase_spellings())


def _lane_occupancy_q(now: datetime, *, cheap: bool | None) -> Q:
    """The rows holding a seat in one cost class's lane at *now*.

    One predicate rather than two: :meth:`TaskQuerySet.cheap_lane_occupancy` reads it as a
    ``COUNT`` and :meth:`TaskQuerySet.record_admission` embeds it in the ``WHERE`` of the
    conditional stamp, and a bound whose probe and whose arbitration disagreed would be no
    bound at all. ``cheap=None`` counts both classes. The two lanes PARTITION the queue —
    the expensive one is the exact complement of the cheap one — so an unregistered
    phase lands in the braked class,
    matching :func:`~teatree.core.modelkit.phases.phase_cost`'s own fail-safe (#4374).
    """
    task_model = cast("type[Task]", apps.get_model("core", "Task"))

    membership = Q() if cheap is None else _cheap_phase_q() if cheap else ~_cheap_phase_q()
    return membership & (
        Q(status=task_model.Status.CLAIMED, lease_expires_at__gt=now)
        | Q(status=task_model.Status.PENDING, admitted_at__gt=now - ADMITTED_INFLIGHT_WINDOW)
    )


def _lane_under_ceiling(now: datetime, ceiling: int, *, cheap: bool | None) -> LessThan:
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
