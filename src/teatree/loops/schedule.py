"""Per-mini-loop next-fire reader for the statusline loop line (#1400).

The statusline's ``loop running · …`` line lists every active cron with its
own next-tick countdown. The infra ``LoopLease`` rows are read in
:mod:`teatree.loop.statusline` directly, but the domain mini-loops
(``dispatch``, ``tickets``, ``review``, ``ship``, ``inbox``,
``resource_pressure``, …) need the mini-loop registry and ``[loops]`` config
— both of which live here in :mod:`teatree.loops`. The tach module graph
forbids :mod:`teatree.loop` from importing :mod:`teatree.loops`, so this
module owns the read and is wired into the statusline via the
:func:`teatree.loop.statusline.set_mini_loop_schedules_reader` injection seam
(installed by the ``loop_tick`` management command), mirroring the
``jobs_builder`` seam :func:`teatree.loop.tick.run_tick` already uses.

The next-fire instant for a loop is the same ``last_fired_at + cadence``
boundary :func:`teatree.loops.gating.elapsed_and_enabled` gates on, so the
statusline countdown stays in lockstep with the orchestrator: a loop reads
``due`` exactly when the gate would fire it.
"""

import datetime as dt
import operator

from teatree.loops.cadence_ledger import MiniLoopMarker
from teatree.loops.config import LoopsConfig
from teatree.loops.registry import iter_loops


def mini_loop_schedules() -> list[tuple[str, dt.datetime | None]]:
    """Return ``(loop_name, next_fire_at)`` for every enabled mini-loop.

    ``next_fire_at`` is the cadence-ledger ``last_fired_at`` plus the loop's
    resolved cadence (:meth:`LoopsConfig.cadence_for`); ``None`` when the loop
    has never fired (no marker row) — the statusline renders that as ``due``.
    Disabled loops are omitted. Sorted by name for a deterministic render.
    """
    config = LoopsConfig.load()
    schedules: list[tuple[str, dt.datetime | None]] = []
    for loop in iter_loops():
        if not config.is_enabled(loop):
            continue
        last_fired_at = _last_fired_at(loop.name)
        if last_fired_at is None:
            schedules.append((loop.name, None))
            continue
        schedules.append((loop.name, last_fired_at + dt.timedelta(seconds=config.cadence_for(loop))))
    return sorted(schedules, key=operator.itemgetter(0))


def _last_fired_at(name: str) -> dt.datetime | None:
    """Return the cadence-ledger ``last_fired_at`` for *name*, or ``None``."""
    marker = MiniLoopMarker.objects.filter(name=name).only("last_fired_at").first()
    return marker.last_fired_at if marker is not None else None


__all__ = ["mini_loop_schedules"]
