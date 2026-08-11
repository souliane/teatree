"""Shared cadence machinery for the periodic task-queuing scanners.

The ``eval_local`` / ``scanning_news`` / ``architectural_review`` /
``backlog_sweep`` / ``triage_assessor`` / ``provision_smoke`` scanners all queue
ONE phase ``Task`` per cadence window, guarded by a pending/claimed dedupe lock
and a ``Session.started_at``-based last-run clock, anchored at a per-overlay
placeholder ticket. :class:`PhaseCadence` owns that shared machinery — the
in-flight dedupe check, the last-run lookup, the bootstrap/cadence trigger
decision, and the placeholder-ticket task write — so each concrete scanner keeps
only its own signal payload plus any genuine variant (``architectural_review``'s
merge-count trigger, ``triage_assessor``'s survivors filter) as its own code.

The task QUERIES live on the ``Task`` manager
(:meth:`TaskQuerySet.in_flight_for_phase` /
:meth:`TaskQuerySet.last_run_at_for_phase`) — this module is the composition seam
between a scanner and those queries, never a second home for the SQL.

Every helper degrades to a no-signal default when the ``core`` models are not yet
registered (a fresh install runs the tick before ``migrate``), preserving the
per-tick fault isolation the loop relies on (``domain_jobs._run_job``).
"""

import datetime as dt
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from django.apps import apps
from django.db import transaction

from teatree.loop.scanners.base import hours_since

if TYPE_CHECKING:
    from teatree.core.models import Session as _Session
    from teatree.core.models import Task as _Task
    from teatree.core.models import Ticket as _Ticket

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PhaseCadence:
    """The once-per-N-hours queue-a-phase-task contract, shared across scanners.

    Constructed per scan from a scanner's ``overlay_name`` / phase constant /
    ``cadence_hours``. Stateless and cheap — a scanner builds one inline rather
    than carrying it as a field, so the scanners stay plain config dataclasses.
    """

    overlay_name: str
    phase: str
    cadence_hours: int

    def in_flight_exists(self) -> bool:
        """True iff a pending/claimed task for this overlay+phase already exists."""
        task_model = _task_model()
        if task_model is None:
            return False
        return task_model.objects.in_flight_for_phase(self.overlay_name, self.phase).exists()

    def last_run_at(self, *, statuses: frozenset[str] | None = None) -> dt.datetime | None:
        """Most recent task's ``Session.started_at``, or ``None`` (bootstrap).

        ``statuses`` narrows to the given ``Task.Status`` set; ``None`` counts a
        task in any status. The ``architectural_review`` variant reads two clocks
        off this via :meth:`last_completed_run_at` and :meth:`last_terminal_run_at`.
        """
        task_model = _task_model()
        if task_model is None:
            return None
        return task_model.objects.last_run_at_for_phase(self.overlay_name, self.phase, statuses=statuses)

    def last_completed_run_at(self) -> dt.datetime | None:
        """Newest COMPLETED run — the success cadence clock (advances only on a review that ran)."""
        task_model = _task_model()
        if task_model is None:
            return None
        return self.last_run_at(statuses=frozenset({task_model.Status.COMPLETED}))

    def last_terminal_run_at(self) -> dt.datetime | None:
        """Newest COMPLETED-or-FAILED run — the post-failure backoff clock."""
        task_model = _task_model()
        if task_model is None:
            return None
        return self.last_run_at(statuses=task_model.Status.terminal())

    def evaluate_trigger(self, *, now: dt.datetime, last_run_at: dt.datetime | None) -> str | None:
        """Return the trigger name (``bootstrap`` / ``cadence``) or ``None``.

        The shared bootstrap+cadence decision. Scanners with an extra trigger
        (``architectural_review``'s merge-count backstop) call this first and add
        their own branch when it returns ``None``.
        """
        if last_run_at is None:
            return "bootstrap"
        if hours_since(last_run_at, now=now) >= self.cadence_hours:
            return "cadence"
        return None

    def queue_task(
        self,
        *,
        placeholder_issue_url: str,
        agent_id: str,
        execution_reason: str,
        subject: str = "",
        log_label: str,
    ) -> "_Task | None":
        """Create a Task + Session row anchored at the per-overlay placeholder ticket.

        Wrapped in ``transaction.atomic()`` so a concurrent scanner on a second
        loop process can't double-queue: the caller's in-flight check and this
        insert run under one DB transaction. A DB error is logged but never
        raised — losing one tick's queue is acceptable; crashing the tick is not.
        Returns the created ``Task`` (or ``None`` when a model is unavailable or
        the write fails).
        """
        ticket_model = _ticket_model()
        task_model = _task_model()
        session_model = _session_model()
        if ticket_model is None or task_model is None or session_model is None:
            return None
        try:
            with transaction.atomic():
                ticket, _created = ticket_model.objects.get_or_create(
                    issue_url=placeholder_issue_url,
                    defaults={"overlay": self.overlay_name, "role": "author"},
                )
                # Keep the overlay tag current — a placeholder ticket that
                # pre-dates the current wiring (e.g. a legacy overlay name) is
                # re-anchored to the canonical name before it queues.
                if ticket.overlay != self.overlay_name:
                    ticket.overlay = self.overlay_name
                    ticket.save(update_fields=["overlay"])
                session = session_model.objects.create(
                    overlay=self.overlay_name,
                    ticket=ticket,
                    agent_id=agent_id,
                )
                return task_model.objects.create(
                    ticket=ticket,
                    session=session,
                    phase=self.phase,
                    subject=subject,
                    execution_reason=execution_reason,
                )
        except Exception:
            logger.exception("%s: failed to queue %s task", log_label, self.phase)
            return None


def _ticket_model() -> "type[_Ticket] | None":
    try:
        return cast("type[_Ticket]", apps.get_model("core", "Ticket"))
    except Exception:  # noqa: BLE001 — a probe failure must never break the tick; degrade to no signal
        return None


def _task_model() -> "type[_Task] | None":
    try:
        return cast("type[_Task]", apps.get_model("core", "Task"))
    except Exception:  # noqa: BLE001 — a probe failure must never break the tick; degrade to no signal
        return None


def _session_model() -> "type[_Session] | None":
    try:
        return cast("type[_Session]", apps.get_model("core", "Session"))
    except Exception:  # noqa: BLE001 — a probe failure must never break the tick; degrade to no signal
        return None


__all__ = [
    "PhaseCadence",
]
