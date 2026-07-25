"""``Task`` queryset helpers for the periodic cadence scanners' dedupe + last-run reads.

Split out of :mod:`teatree.core.managers` (module-health LOC budget), mirroring
the ``managers_overlay`` / ``managers_issue_match`` concern modules. Referenced by
:meth:`TaskQuerySet.in_flight_for_phase` / :meth:`TaskQuerySet.last_run_at_for_phase`
— the single home for the queries the ``PhaseCadence`` scanner helper composes.
"""

from datetime import datetime
from typing import TYPE_CHECKING, cast

from django.apps import apps
from django.db import models
from django.db.models import Max

if TYPE_CHECKING:
    from teatree.core.models.task import Task


def in_flight_for_phase(qs: models.QuerySet, overlay: str, phase: str) -> models.QuerySet:
    """Pending/claimed tasks for one overlay+phase — the scanners' dedupe lock.

    ``Status.active()`` (PENDING|CLAIMED) is the SSOT for "in flight", so no
    scanner carries a private ``{"pending","claimed"}`` constant.
    """
    task_model = cast("type[Task]", apps.get_model("core", "Task"))
    return qs.filter(ticket__overlay=overlay, phase=phase, status__in=task_model.Status.active())


def last_run_at_for_phase(
    qs: models.QuerySet, overlay: str, phase: str, *, completed_only: bool = False
) -> datetime | None:
    """Most recent ``Session.started_at`` for an overlay+phase task, or ``None``.

    A ``Task`` always carries a ``Session`` created at queue time, so the newest
    ``session__started_at`` is the last-run instant; ``None`` is the bootstrap
    case. ``completed_only`` narrows to COMPLETED tasks — the
    ``architectural_review`` variant, whose cadence only advances on a review that
    actually ran (a FAILED task must not suppress the next dispatch).
    """
    task_model = cast("type[Task]", apps.get_model("core", "Task"))
    scoped = qs.filter(ticket__overlay=overlay, phase=phase)
    if completed_only:
        scoped = scoped.filter(status=task_model.Status.COMPLETED)
    return scoped.aggregate(ts=Max("session__started_at"))["ts"]
