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


class DreamRunMarkerManager(models.Manager["DreamRunMarker"]):
    """Read/write surface for the dream-run cadence + staleness alarm."""

    def mark_succeeded(self, ts: dt.datetime) -> None:
        """Stamp both attempted and succeeded at *ts* — a clean consolidation run."""
        self.update_or_create(
            name=DreamRunMarker.NAME,
            defaults={"last_attempted_at": ts, "last_succeeded_at": ts},
        )

    def mark_attempted(self, ts: dt.datetime) -> None:
        """Stamp the attempt at *ts* without touching ``last_succeeded_at``.

        A failed run bumps only the attempt timestamp, so :meth:`is_stale`
        (which keys on success) still fires when attempts keep failing.
        """
        self.update_or_create(
            name=DreamRunMarker.NAME,
            defaults={"last_attempted_at": ts},
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


class DreamRunMarker(models.Model):
    """The single dream-run cadence/staleness marker row."""

    NAME: ClassVar[str] = "dream"

    name = models.CharField(max_length=64, unique=True, default=NAME)
    last_succeeded_at = models.DateTimeField(null=True, blank=True)
    last_attempted_at = models.DateTimeField(null=True, blank=True)

    objects: ClassVar[DreamRunMarkerManager] = DreamRunMarkerManager()

    class Meta:
        db_table = "teatree_dream_run_marker"

    def __str__(self) -> str:
        succeeded = self.last_succeeded_at.isoformat() if self.last_succeeded_at else "never"
        return f"dream-run<{self.name}:succeeded={succeeded}>"
