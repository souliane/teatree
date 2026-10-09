"""The append-only record of one ticket sweep, and the count that must tend to zero (#162).

Rule 4 of #162: "each sweep tends to zero changes". That only works
as a metric if a sweep that changed nothing leaves evidence behind — without a
row, "healthy factory" and "the sweep never ran" look identical, and the trend
the owner is supposed to read is unreadable. So :meth:`TicketSweepRunManager.finish`
persists a zero as a result, and a run that crashed stays visibly *incomplete*
rather than vanishing.

``changed_count`` is derived from the set of changed issue URLs rather than being
incremented, because a sweep that folds two comments into one ticket changed ONE
ticket. An incrementing counter would report the number of writes and drift away
from the thing being measured the moment a fold takes two passes.
"""

import uuid
from typing import ClassVar

from django.db import models, transaction
from django.utils import timezone

from teatree.core.modelkit.db_retry import retry_on_locked

SOURCE_INTERACTIVE = "interactive"
SOURCE_LOOP = "loop"
_SOURCES = (SOURCE_INTERACTIVE, SOURCE_LOOP)


class TicketSweepRunManager(models.Manager["TicketSweepRun"]):
    """The guarded surface — a run is begun, changed, and finished, never edited."""

    def begin(self, *, source: str, overlay: str = "") -> "TicketSweepRun":
        """Open a run and return it. *source* says who is sweeping."""
        if source not in _SOURCES:
            msg = f"source must be one of {_SOURCES}, got {source!r}"
            raise ValueError(msg)
        return retry_on_locked(
            lambda: self.create(run_id=uuid.uuid4().hex, source=source, overlay=overlay),
        )

    def record_change(self, *, run_id: str, issue_url: str) -> "TicketSweepRun":
        """Attribute a landed ticket mutation to *run_id*, idempotently.

        Re-recording the same URL is a no-op, so a retried fold cannot inflate
        the count the owner reads as a regression signal.
        """

        def _record() -> TicketSweepRun:
            with transaction.atomic():
                run = self.select_for_update().get(run_id=run_id)
                if run.finished_at is not None:
                    msg = f"sweep run {run_id} is already finished; it cannot record further changes"
                    raise ValueError(msg)
                if issue_url and issue_url not in run.changed_urls:
                    run.changed_urls = [*run.changed_urls, issue_url]
                    run.save(update_fields=["changed_urls"])
                return run

        return retry_on_locked(_record)

    def finish(self, *, run_id: str, examined_count: int = 0, external_skipped_count: int = 0) -> "TicketSweepRun":
        """Close *run_id*. Refuses an unknown run and refuses to re-close a finished one."""

        def _finish() -> TicketSweepRun:
            with transaction.atomic():
                run = self.select_for_update().get(run_id=run_id)
                if run.finished_at is not None:
                    msg = f"sweep run {run_id} is already finished"
                    raise ValueError(msg)
                run.finished_at = timezone.now()
                run.examined_count = max(0, int(examined_count))
                run.external_skipped_count = max(0, int(external_skipped_count))
                run.save(update_fields=["finished_at", "examined_count", "external_skipped_count"])
                return run

        return retry_on_locked(_finish)

    def recent(self, *, limit: int = 10) -> models.QuerySet["TicketSweepRun"]:
        """The last *limit* FINISHED runs, newest first — the trend series."""
        return self.exclude(finished_at=None).order_by("-finished_at")[:limit]

    def incomplete(self) -> models.QuerySet["TicketSweepRun"]:
        """Runs that were begun and never closed — a crashed or abandoned sweep."""
        return self.filter(finished_at=None)

    def zero_streak(self) -> int:
        """How many consecutive finished runs, counting back from the latest, changed nothing.

        The healthy direction: rules 1 and 2 stop the drift at the source, so the
        streak grows. A streak that breaks is the signal to look at what filed a
        requirement into a comment again.
        """
        streak = 0
        for run in self.exclude(finished_at=None).order_by("-finished_at"):
            if run.changed_count:
                break
            streak += 1
        return streak


class TicketSweepRun(models.Model):
    """One ticket-hygiene sweep: what it examined, what it changed, whether it finished."""

    run_id = models.CharField(max_length=64, unique=True)
    source = models.CharField(max_length=16, default=SOURCE_INTERACTIVE)
    overlay = models.CharField(max_length=64, blank=True, default="")
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    examined_count = models.PositiveIntegerField(default=0)
    external_skipped_count = models.PositiveIntegerField(default=0)
    changed_urls = models.JSONField(default=list)

    objects: ClassVar[TicketSweepRunManager] = TicketSweepRunManager()

    class Meta:
        db_table = "teatree_ticket_sweep_run"
        ordering: ClassVar = ["-started_at"]

    def __str__(self) -> str:
        state = "open" if self.finished_at is None else f"changed:{self.changed_count}"
        return f"ticket-sweep<{self.run_id[:8]} {self.source} {state}>"

    @property
    def changed_count(self) -> int:
        """Tickets this run changed — unique URLs, so two folds on one ticket count once."""
        return len(set(self.changed_urls))


__all__ = ["SOURCE_INTERACTIVE", "SOURCE_LOOP", "TicketSweepRun", "TicketSweepRunManager"]
