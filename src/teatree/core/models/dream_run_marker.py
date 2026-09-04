"""Staleness alarm source for the idle-time dream engine (#1933).

A single :class:`DreamRunMarker` row (``name = "dream"``) carries the last
successful and last attempted consolidation timestamps. The loop reads
:meth:`DreamRunMarkerManager.is_stale` to decide whether memory
consolidation has not succeeded within the cadence window and raise the
staleness alarm — an attempt that keeps failing leaves ``last_succeeded_at``
behind ``last_attempted_at``, so staleness keys on the *success* timestamp.

Mirror shape of :class:`teatree.core.models.self_update_marker.SelfUpdateMarker`
— a single ``name`` identity carrying timestamps, with the read/write surface
on the manager.
"""

import datetime as dt
from typing import ClassVar

from django.db import models

STALE_THRESHOLD_HOURS = 48

#: How many staleness windows a once-working pass may miss before the alarm stops being
#: advisory. One missed window is a quiet night; this many is something structurally
#: blocking every pass, which nobody notices while the only signal is a WARN (#3993).
CRITICAL_STALE_MULTIPLE = 3

#: The pass reached its gates and stamped success.
OUTCOME_STAMPED = "stamped"
#: The pass reached a verdict and the acceptance gates REFUSED it.
OUTCOME_GATES_FAILED = "gates_failed"
#: The pass ran to a terminal verdict that was neither of the above (0 members, a raised
#: pass, a broken distiller batch).
OUTCOME_FAILED = "failed"


class DreamRunMarkerManager(models.Manager["DreamRunMarker"]):
    """Read/write surface for the dream-run cadence + staleness alarm."""

    def mark_succeeded(self, ts: dt.datetime) -> None:
        """Stamp both attempted and succeeded at *ts* — a clean consolidation run."""
        self.update_or_create(
            name=DreamRunMarker.NAME,
            defaults={
                "last_attempted_at": ts,
                "last_succeeded_at": ts,
                "last_outcome": OUTCOME_STAMPED,
                "last_failure_detail": "",
            },
        )

    def mark_attempted(self, ts: dt.datetime, *, outcome: str = "", failure_detail: str = "") -> None:
        """Stamp the attempt at *ts* without touching ``last_succeeded_at``.

        A failed run bumps only the attempt timestamp, so :meth:`is_stale`
        (which keys on success) still fires when attempts keep failing.

        *outcome* is the pass's TERMINAL verdict, and its ABSENCE is the load-bearing
        signal (#4671): the attempt anchor is stamped BEFORE the pass so a SIGKILLed pass
        still moves it (#4355), which left a killed pass indistinguishable from a gate
        refusal — the doctor asserted "every pass is being withheld" when no verdict had
        been reached at all. A pre-pass stamp therefore CLEARS the previous outcome, so a
        pass that dies mid-flight leaves it blank and the killed case is nameable.
        """
        self.update_or_create(
            name=DreamRunMarker.NAME,
            defaults={"last_attempted_at": ts, "last_outcome": outcome, "last_failure_detail": failure_detail},
        )

    def is_stale(self, now: dt.datetime, threshold_hours: int = STALE_THRESHOLD_HOURS) -> bool:
        """True iff consolidation has not succeeded within ``threshold_hours``.

        On bootstrap (no marker row, or a row that never succeeded) the
        engine is treated as stale — it has never produced a successful run,
        which is exactly the condition the alarm should surface.
        """
        marker = self.filter(name=DreamRunMarker.NAME).first()
        if marker is None or marker.last_succeeded_at is None:
            return True
        threshold = dt.timedelta(hours=threshold_hours)
        return (now - marker.last_succeeded_at) >= threshold

    def is_critically_stale(
        self,
        now: dt.datetime,
        threshold_hours: int = STALE_THRESHOLD_HOURS,
        multiple: int = CRITICAL_STALE_MULTIPLE,
    ) -> bool:
        """True iff a pass that HAS succeeded before has not for *multiple* windows.

        The hard tier under :meth:`is_stale`'s advisory WARN. Bootstrap — no row, or a
        row that never succeeded — is deliberately excluded: there is no baseline to
        regress from, so a fresh install must not redden ``t3 doctor check``.
        """
        marker = self.filter(name=DreamRunMarker.NAME).first()
        if marker is None or marker.last_succeeded_at is None:
            return False
        return (now - marker.last_succeeded_at) >= dt.timedelta(hours=threshold_hours * multiple)


class DreamRunMarker(models.Model):
    """The single dream-run cadence/staleness marker row."""

    NAME: ClassVar[str] = "dream"

    name = models.CharField(max_length=64, unique=True, default=NAME)
    last_succeeded_at = models.DateTimeField(null=True, blank=True)
    last_attempted_at = models.DateTimeField(null=True, blank=True)
    #: Where the next pass resumes in the ranked corpus when the per-pass batch cap
    #: binds. Without it a capped pass re-distils the ranking's head every night and
    #: the tail is never consolidated at all; see
    #: :func:`teatree.loops.dream.distill.distill_in_batches`.
    distill_cursor = models.PositiveIntegerField(default=0)
    #: The last pass's TERMINAL verdict, blank when it never reached one. Blank-after-an-
    #: attempt is what distinguishes a pass SIGKILLed mid-flight from one whose gates
    #: refused it — see :meth:`DreamRunMarkerManager.mark_attempted`.
    last_outcome = models.CharField(max_length=32, blank=True, default="")
    #: The failing gates' rendered detail when *last_outcome* is a refusal, so the doctor
    #: quotes WHICH gate refused instead of sending the reader to re-run the pass.
    last_failure_detail = models.TextField(blank=True, default="")

    objects: ClassVar[DreamRunMarkerManager] = DreamRunMarkerManager()

    class Meta:
        db_table = "teatree_dream_run_marker"

    def __str__(self) -> str:
        succeeded = self.last_succeeded_at.isoformat() if self.last_succeeded_at else "never"
        return f"dream-run<{self.name}:succeeded={succeeded}>"
