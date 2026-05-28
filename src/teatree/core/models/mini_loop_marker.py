"""Per-mini-loop cadence ledger (#1432).

One :class:`MiniLoopMarker` row per loop name records the last time the
:class:`teatree.loops.orchestrator.Orchestrator` fired the mini-loop. The
orchestrator checks ``elapsed_since(name)`` against the loop's configured
cadence before each tick; if the gate is open, the loop runs and the
marker is bumped via ``mark_fired(name, ts)``.

Mirror shape of :class:`teatree.core.models.pull_main_clone_marker.PullMainCloneMarker`
and :class:`teatree.core.models.self_update_marker.SelfUpdateMarker` — a
single ``name`` identity carrying a ``last_fired_at`` timestamp.
"""

import datetime as dt
from typing import ClassVar

from django.db import models


class MiniLoopMarkerManager(models.Manager["MiniLoopMarker"]):
    """Manager for the orchestrator's cadence-gate read/write surface."""

    def mark_fired(self, name: str, ts: dt.datetime) -> None:
        """Upsert the marker row for *name* with ``last_fired_at = ts``.

        Called by the orchestrator after dispatching a mini-loop so the
        next tick reads the bumped timestamp instead of re-firing. Uses
        ``update_or_create`` so the row is created on first fire and
        updated thereafter — concurrent ticks racing on the same name
        last-write-wins (the orchestrator is a per-session singleton via
        the §786 WS1 LoopLease so concurrent fire is structurally
        impossible, but the upsert is the defensive shape regardless).
        """
        self.update_or_create(name=name, defaults={"last_fired_at": ts})

    def elapsed_since(self, name: str, now: dt.datetime) -> float | None:
        """Return seconds since the last fire, or ``None`` on bootstrap.

        ``None`` means "no marker row exists" — the loop has never fired
        on this install. The orchestrator treats ``None`` as
        cadence-elapsed (fire immediately) so a fresh install does not
        wait one cadence-window before the first dispatch.
        """
        marker = self.filter(name=name).first()
        if marker is None:
            return None
        return (now - marker.last_fired_at).total_seconds()


class MiniLoopMarker(models.Model):
    """One row per mini-loop name carrying the last-fired timestamp."""

    name = models.CharField(max_length=64, unique=True)
    last_fired_at = models.DateTimeField()

    objects: ClassVar[MiniLoopMarkerManager] = MiniLoopMarkerManager()

    class Meta:
        db_table = "teatree_mini_loop_marker"
        ordering: ClassVar = ["-last_fired_at"]

    def __str__(self) -> str:
        return f"mini-loop<{self.name}@{self.last_fired_at.isoformat()}>"
