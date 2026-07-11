"""Periodic local-eval scanner.

The user directive (2026-06-05): "AI evals should be run locally from
time to time, and in CI once a week." The CI half already exists
(``.github/workflows/ci.yml`` ``eval-weekly`` + ``scripts/eval/
first_pr_of_week.py``). This is the local half: the loop fires an
``eval_local`` task per cadence window (default 168h = weekly) so the
SCOPED eval suite runs locally without depending on an external cron.

The scanner mirrors :mod:`teatree.loop.scanners.scanning_news`:

* **Single trigger.** Only a cadence (``eval_local_cadence_hours``,
    default 168h). A fixed-rate platform behaviour, not coupled to
    delivery velocity.
* **Overlay anchor is injected, not baked.** A core scanner that does
    not know any overlay's name; the wiring layer
    (``teatree.loop.global_scanner_factories._eval_local_scanner``) resolves the active
    overlay via :func:`teatree.config.discover_active_overlay`.
* **Same dedup contract.** A pending or claimed ``eval_local`` task acts
    as the lock — completion (or failure) unlocks the next cadence
    window. No new model fields; the most recent task's
    ``Session.started_at`` is the "last run" timestamp.
* **Non-blocking.** ``scan()`` only writes the Task row and returns; the
    dispatcher routes it through the standard pending-task pipeline. The
    queued task's directive runs the local transcript runner (the same
    one ``t3 eval run`` defaults to — $0 extra, runs no model), so the
    long-running suite never blocks the tick.
"""

import datetime as dt
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from django.apps import apps
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from teatree.loop.scanners.base import ScanSignal, hours_since

if TYPE_CHECKING:
    from teatree.core.models import Session as _Session
    from teatree.core.models import Task as _Task
    from teatree.core.models import Ticket as _Ticket

logger = logging.getLogger(__name__)

#: Canonical phase token written to ``Task.phase`` for local-eval tasks.
EVAL_LOCAL_PHASE = "eval_local"

#: States that mean an eval task is still in-flight (cannot queue a dupe).
_IN_FLIGHT_TASK_STATES: frozenset[str] = frozenset({"pending", "claimed"})


@dataclass(slots=True)
class EvalLocalScanner:
    """Queue a periodic ``eval_local`` task for the active core overlay.

    Configuration fields are passed explicitly (rather than read from a
    global at scan time) so test setup is deterministic and the wiring
    layer is the single place that resolves
    :class:`teatree.config.UserSettings` and
    :func:`teatree.config.discover_active_overlay` to scanner kwargs. The
    on/off decision lives at the wiring layer (``eval_local_disabled`` in
    core config); the scanner itself always scans when invoked.
    """

    overlay_name: str
    skill: str = "eval"
    cadence_hours: int = 168
    name: str = "eval_local"

    def scan(self) -> list[ScanSignal]:
        if self._in_flight_task_exists():
            return []

        now = timezone.now()
        last_run_at = self._last_run_at()
        trigger = self._evaluate_trigger(now=now, last_run_at=last_run_at)
        if trigger is None:
            return []

        task = self._queue_task(trigger=trigger)
        if task is None:
            return []
        return [
            ScanSignal(
                kind="eval_local.queued",
                summary=f"local eval queued for {self.overlay_name} (trigger: {trigger})",
                payload={
                    "overlay": self.overlay_name,
                    "skill": self.skill,
                    "phase": EVAL_LOCAL_PHASE,
                    "task_id": task.pk,
                    "trigger": trigger,
                },
            ),
        ]

    def _in_flight_task_exists(self) -> bool:
        task_model = _task_model()
        if task_model is None:
            return False
        return task_model.objects.filter(
            ticket__overlay=self.overlay_name,
            phase=EVAL_LOCAL_PHASE,
            status__in=_IN_FLIGHT_TASK_STATES,
        ).exists()

    def _last_run_at(self) -> dt.datetime | None:
        task_model = _task_model()
        if task_model is None:
            return None
        aggregate = task_model.objects.filter(
            ticket__overlay=self.overlay_name,
            phase=EVAL_LOCAL_PHASE,
        ).aggregate(ts=Max("session__started_at"))
        return aggregate["ts"]

    def _evaluate_trigger(self, *, now: dt.datetime, last_run_at: dt.datetime | None) -> str | None:
        if last_run_at is None:
            return "bootstrap"
        if hours_since(last_run_at, now=now) >= self.cadence_hours:
            return "cadence"
        return None

    def _queue_task(self, *, trigger: str) -> "_Task | None":
        ticket_model = _ticket_model()
        task_model = _task_model()
        session_model = _session_model()
        if ticket_model is None or task_model is None or session_model is None:
            return None
        try:
            with transaction.atomic():
                ticket, _created = ticket_model.objects.get_or_create(
                    issue_url=self._placeholder_issue_url(),
                    defaults={"overlay": self.overlay_name, "role": "author"},
                )
                if ticket.overlay != self.overlay_name:
                    ticket.overlay = self.overlay_name
                    ticket.save(update_fields=["overlay"])
                session = session_model.objects.create(
                    overlay=self.overlay_name,
                    ticket=ticket,
                    agent_id=f"eval-local-{self.overlay_name}",
                )
                return task_model.objects.create(
                    ticket=ticket,
                    session=session,
                    phase=EVAL_LOCAL_PHASE,
                    execution_target=task_model.ExecutionTarget.HEADLESS,
                    execution_reason=self._execution_reason(trigger),
                )
        except Exception:
            logger.exception("EvalLocalScanner: failed to queue eval_local task")
            return None

    def _execution_reason(self, trigger: str) -> str:
        """Direct the SCOPED local run via the $0-extra transcript runner.

        The dispatched skill reads ``execution_reason``; the ``t3 eval
        run`` + ``transcript`` substrings are load-bearing — they tell
        the skill to run the same scoped, $0-extra path the user runs
        by hand (``t3 eval run`` defaults to the transcript backend),
        plus the deterministic ``pinned-regressions`` check.
        """
        return (
            f"Periodic local eval ({trigger}) via skill: {self.skill} | run the SCOPED suite locally with "
            "`t3 eval pinned-regressions` and `t3 eval run` "
            "(transcript backend, $0 extra)"
        )

    def _placeholder_issue_url(self) -> str:
        return f"eval-local://{self.overlay_name}"


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
    "EVAL_LOCAL_PHASE",
    "EvalLocalScanner",
]
