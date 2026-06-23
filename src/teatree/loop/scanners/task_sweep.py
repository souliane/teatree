"""Task-sweep scanner — verify open teatree Task rows against artifact terminal state (#129).

This scanner reconciles teatree **Task** rows (the DB-backed lifecycle work
units), never the agent harness's working list of session items. The candidate
set is the open :class:`Task` rows (status ``PENDING`` or ``CLAIMED``) whose
``Ticket`` carries an ``issue_url``. Each tick the scanner walks those tasks
and, for each, verifies the underlying artifact's *terminal* state — is the
upstream issue closed / the PR merged? — via the overlay's ``is_issue_done()``
hook (the same independent-evidence check ``TicketCompletionScanner`` uses for
post-ship tickets). Completion is driven by **durable artifact proof**, never a
self-reported claim, and never in bulk: each task is verified individually
before its FSM advances.

Per-item outcome. A **terminal** artifact (issue closed / PR merged) emits
``task.completion_detected`` → the ``task_completion`` mechanical handler
RE-checks terminal state, then completes the task (advancing the ticket FSM via
``Task.complete``). A **network/auth error or no code host** emits
``task.orphaned`` (statusline-only) — fail-OPEN: never auto-complete on
uncertainty, surface for operator review. **Otherwise** no signal is emitted
(the task is genuinely still open).

Idempotency: an atomic conditional ``UPDATE`` stamps ``Task.last_sweep_check_ts``
before each task is verified, so two concurrent ticks never double-process the
same task. A task swept within ``recheck_interval_hours`` is skipped. The whole
scan tolerates a missing table (pre-migration install) and any per-task failure
without crashing the tick (mirrors ``SelfUpdateScanner`` / ``IncomingEventsScanner``).
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, cast

from django.apps import apps
from django.db import OperationalError, ProgrammingError
from django.utils import timezone

from teatree.backends.loader import get_code_host_for_url
from teatree.core.overlay import OverlayBase
from teatree.loop.scanners.base import ScanSignal

if TYPE_CHECKING:
    from teatree.core.models import Task

logger = logging.getLogger(__name__)

_OPEN_STATUSES = ("pending", "claimed")


@dataclass(slots=True)
class TaskSweepScanner:
    """Verify open teatree Task rows against artifact terminal state and advance the FSM.

    ``overlay`` is the active :class:`OverlayBase` (its ``is_issue_done`` hook
    is the terminal-state oracle); ``overlay_name`` scopes the candidate query
    to one overlay's tasks. ``recheck_interval_hours`` is the per-task
    anti-thrash window — a task verified within the window is skipped this tick.
    The scanner is a pure signal collector; the ``task_completion`` mechanical
    handler performs the FSM transition (with its own re-check).
    """

    overlay: OverlayBase
    overlay_name: str = ""
    recheck_interval_hours: int = 1
    name: str = "task_sweep"

    def scan(self) -> list[ScanSignal]:
        signals: list[ScanSignal] = []
        now = timezone.now()
        for task in self._candidate_tasks(now):
            try:
                if not self._claim_for_sweep(task_id=task.pk, now=now):
                    # Another concurrent tick won the atomic stamp — skip this task.
                    continue
                signal = self._verify(task)
            except Exception:
                logger.exception("TaskSweepScanner failed on task %s", task.pk)
                continue
            if signal is not None:
                signals.append(signal)
        return signals

    def _candidate_tasks(self, now: datetime) -> list["Task"]:
        task_model = cast("type[Task]", apps.get_model("core", "Task"))
        cutoff = now - timedelta(hours=self.recheck_interval_hours)
        from django.db.models import Q  # noqa: PLC0415

        qs = (
            task_model.objects.filter(status__in=_OPEN_STATUSES)
            .exclude(ticket__issue_url="")
            .filter(Q(last_sweep_check_ts__isnull=True) | Q(last_sweep_check_ts__lt=cutoff))
            .select_related("ticket")
        )
        if self.overlay_name:
            qs = qs.filter(ticket__overlay=self.overlay_name)
        try:
            return list(qs.only("id", "status", "ticket__id", "ticket__issue_url", "last_sweep_check_ts"))
        except (OperationalError, ProgrammingError):
            # Pre-migration install: the table or the new column does not exist
            # yet. The next tick after ``migrate`` picks the tasks up.
            return []

    def _claim_for_sweep(self, *, task_id: int, now: datetime) -> bool:
        """Atomically stamp ``last_sweep_check_ts``; True iff this tick won the race."""
        from django.db.models import Q  # noqa: PLC0415

        task_model = cast("type[Task]", apps.get_model("core", "Task"))
        cutoff = now - timedelta(hours=self.recheck_interval_hours)
        try:
            updated = task_model.objects.filter(
                Q(last_sweep_check_ts__isnull=True) | Q(last_sweep_check_ts__lt=cutoff),
                pk=task_id,
            ).update(last_sweep_check_ts=now)
        except (OperationalError, ProgrammingError):
            return False
        return updated == 1

    def _verify(self, task: "Task") -> ScanSignal | None:
        ticket = task.ticket
        host = get_code_host_for_url(self.overlay, ticket.issue_url)
        if host is None:
            return self._orphaned_signal(task)
        try:
            issue_data = host.get_issue(ticket.issue_url)
        except Exception:  # noqa: BLE001 — any host error is fail-OPEN, never auto-complete.
            logger.warning("task_sweep: failed to fetch issue for task %s (%s)", task.pk, ticket.issue_url)
            return self._orphaned_signal(task)
        if not isinstance(issue_data, dict) or "error" in issue_data:
            return self._orphaned_signal(task)
        if self.overlay.is_issue_done(issue_data):
            return ScanSignal(
                kind="task.completion_detected",
                summary=f"Task {task.pk} — artifact done upstream ({ticket.ticket_number})",
                payload={
                    "task_id": task.pk,
                    "ticket_id": ticket.pk,
                    "issue_url": ticket.issue_url,
                    "overlay": self.overlay_name,
                },
            )
        return None

    def _orphaned_signal(self, task: "Task") -> ScanSignal:
        return ScanSignal(
            kind="task.orphaned",
            summary=f"Task {task.pk} — artifact state unverifiable, needs operator review",
            payload={
                "task_id": task.pk,
                "ticket_id": task.ticket.pk,
                "issue_url": task.ticket.issue_url,
                "overlay": self.overlay_name,
            },
        )


__all__ = ["TaskSweepScanner"]
