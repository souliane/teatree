"""Periodic backlog-sweep scanner — #2419.

Companion to the ``sweeping-tickets`` skill: once the sweep's verdicts prove
trustworthy, the loop fires a low-frequency ``backlog_sweep`` task that
consolidates the issue tracker (shipped / consolidate-into-epic /
regressive / still-standalone against current ``main``) without depending
on an external cron. The scanner mirrors the shape of
:mod:`teatree.loop.scanners.scanning_news` but bakes in two safety
properties from day one because the sweep is destructive-capable (it can
propose closing issues):

* **Default-OFF.** Unlike the always-on news/eval scanners, the kill
    switch (``backlog_sweep_disabled``) defaults *on* at the wiring layer,
    so the scanner is inert until the user opts in. This module always
    scans when invoked — the on/off decision lives at the wiring layer
    (``teatree.loop.global_scanner_factories._backlog_sweep_scanner``).
* **Ask-gate in the directive.** The queued task carries an ASK-GATE
    marker so the dispatched sweep records close/fold proposals and
    surfaces the batch for explicit approval — it never mass-closes or
    mass-folds unattended. Only the high-confidence shipped-by-merged-PR
    class auto-closes (the skill's own discipline).

Other invariants mirror ``scanning_news``:

* **Single trigger.** Only a cadence (``backlog_sweep_cadence_hours``,
    default 168h = weekly). A fixed-rate platform behaviour, not coupled
    to delivery velocity.
* **Overlay anchor is injected, not baked.** A core scanner that does not
    know any overlay's name; the wiring layer resolves the active core
    overlay via :func:`teatree.config.discover_active_overlay` and passes
    the result as the ``overlay_name`` constructor kwarg.
* **Same dedup contract.** A pending or claimed ``backlog_sweep`` task
    acts as the lock — completion (or failure) unlocks the next cadence
    window. No new model fields; the most recent task's
    ``Session.started_at`` is the "last run" timestamp.
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

#: Canonical phase token written to ``Task.phase`` for backlog-sweep tasks.
BACKLOG_SWEEP_PHASE = "backlog_sweep"

#: States that mean a sweep task is still in-flight (cannot queue a dupe).
_IN_FLIGHT_TASK_STATES: frozenset[str] = frozenset({"pending", "claimed"})


@dataclass(slots=True)
class BacklogSweepScanner:
    """Queue a periodic ``backlog_sweep`` task for the active core overlay.

    Configuration fields are passed explicitly (rather than read from a
    global at scan time) so test setup is deterministic and the wiring
    layer is the single place that resolves
    :class:`teatree.config.UserSettings` and
    :func:`teatree.config.discover_active_overlay` to scanner kwargs. The
    on/off decision lives at the wiring layer (``backlog_sweep_disabled``
    in core config, defaulting ON); the scanner itself always scans when
    invoked.

    ``overlay_name`` is the resolved overlay-anchor identity for the
    placeholder ticket. The scanner never reads or assumes the name — it
    stamps whatever value the wiring layer hands it. The canonical default
    in production is ``"t3-teatree"``.

    ``require_approval`` is the ask-gate flag, resolved from
    ``ask_before_backlog_sweep_closes`` at the wiring layer. When true
    (the default), the queued task's directive instructs the dispatched
    skill to record each close proposal and surface the batch for explicit
    user approval — it must NOT mass-close unattended. The scanner never
    closes issues itself; this flag is the contract it stamps onto the
    task so the skill cannot silently fall back to bulk closing.
    """

    overlay_name: str
    skill: str = "sweeping-tickets"
    cadence_hours: int = 168
    require_approval: bool = True
    name: str = "backlog_sweep"

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
                kind="backlog_sweep.queued",
                summary=f"backlog-sweep queued for {self.overlay_name} (trigger: {trigger})",
                payload={
                    "overlay": self.overlay_name,
                    "skill": self.skill,
                    "phase": BACKLOG_SWEEP_PHASE,
                    "task_id": task.pk,
                    "trigger": trigger,
                    "require_approval": self.require_approval,
                },
            ),
        ]

    def _in_flight_task_exists(self) -> bool:
        """True iff a pending/claimed backlog-sweep task already exists."""
        task_model = _task_model()
        if task_model is None:
            return False
        return task_model.objects.filter(
            ticket__overlay=self.overlay_name,
            phase=BACKLOG_SWEEP_PHASE,
            status__in=_IN_FLIGHT_TASK_STATES,
        ).exists()

    def _last_run_at(self) -> dt.datetime | None:
        """Return the most recent task's Session.started_at, or None.

        ``None`` when no prior backlog-sweep task has been recorded — the
        bootstrap case where the cadence is trivially elapsed.
        """
        task_model = _task_model()
        if task_model is None:
            return None
        aggregate = task_model.objects.filter(
            ticket__overlay=self.overlay_name,
            phase=BACKLOG_SWEEP_PHASE,
        ).aggregate(ts=Max("session__started_at"))
        return aggregate["ts"]

    def _evaluate_trigger(self, *, now: dt.datetime, last_run_at: dt.datetime | None) -> str | None:
        """Return the trigger name (``bootstrap`` / ``cadence``) or None."""
        if last_run_at is None:
            return "bootstrap"
        if hours_since(last_run_at, now=now) >= self.cadence_hours:
            return "cadence"
        return None

    def _queue_task(self, *, trigger: str) -> "_Task | None":
        """Create a Task + Session row anchored at the overlay placeholder ticket.

        Wrapped in ``transaction.atomic()`` so a concurrent scanner on a
        second loop process can't double-queue: the in-flight check and
        the insert run under one DB transaction. A DB error is logged but
        never raised — losing one tick's sweep queue is acceptable;
        crashing the tick is not.
        """
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
                    agent_id=f"backlog-sweep-{self.overlay_name}",
                )
                return task_model.objects.create(
                    ticket=ticket,
                    session=session,
                    phase=BACKLOG_SWEEP_PHASE,
                    execution_target=task_model.ExecutionTarget.HEADLESS,
                    execution_reason=self._execution_reason(trigger),
                )
        except Exception:
            logger.exception("BacklogSweepScanner: failed to queue backlog-sweep task")
            return None

    def _execution_reason(self, trigger: str) -> str:
        """Build the dispatcher directive, embedding the ask-gate contract.

        When ``require_approval`` is on (the default), the directive
        carries an explicit instruction that the skill must record each
        close proposal and surface the batch for user approval — it must
        NOT mass-close issues unattended. It also routes the one
        auto-closable class through the gated ``ticket bulk-close`` command
        so the no-bulk-close gate (:mod:`teatree.core.gates.bulk_close_gate`)
        applies to the autonomous close path exactly as it does to a manual
        CLI one. The marker substrings are load-bearing: they are the
        channel the dispatched skill reads to know the gate is active.
        """
        base = f"Periodic backlog-sweep triage ({trigger}) via skill: {self.skill}"
        if self.require_approval:
            return (
                f"{base} | ASK-GATE: do NOT mass-close issues unattended — record each close "
                "proposal with its citation and surface the batch for explicit user approval; "
                "only the high-confidence merged-PR-superseded class auto-closes, and that "
                "auto-close MUST go through `t3 <overlay> ticket bulk-close --ids <ids> --confirm <ids>` "
                "(which enforces the no-bulk-close gate) — never a raw per-item `ticket ignore` loop "
                "(#2419, #1931)"
            )
        return base

    def _placeholder_issue_url(self) -> str:
        """Stable synthetic URL for the overlay-anchored placeholder ticket."""
        return f"backlog-sweep://{self.overlay_name}"


def _ticket_model() -> "type[_Ticket] | None":
    try:
        return cast("type[_Ticket]", apps.get_model("core", "Ticket"))
    except Exception:  # noqa: BLE001
        return None


def _task_model() -> "type[_Task] | None":
    try:
        return cast("type[_Task]", apps.get_model("core", "Task"))
    except Exception:  # noqa: BLE001
        return None


def _session_model() -> "type[_Session] | None":
    try:
        return cast("type[_Session]", apps.get_model("core", "Session"))
    except Exception:  # noqa: BLE001
        return None


__all__ = [
    "BACKLOG_SWEEP_PHASE",
    "BacklogSweepScanner",
]
