"""Per-mini-loop next-fire reader for the statusline loop line (#1400).

The statusline's dedicated loop line lists every active cron with its
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

The next-fire instant comes from :func:`teatree.loops.live.build_report` — the
same live snapshot ``t3 loop list`` renders (#1744), computed from the ``Loop``
table's ``last_run_at`` cadence anchor — so the statusline countdown,
``t3 loop list``, and the master tick gate
(:func:`teatree.loops.master.build_loop_table_jobs` via ``Loop.is_due``) all read
one source of truth: a loop reads ``due`` exactly when the gate would fire it.
"""

import datetime as dt

from teatree.loops.live import build_report


def mini_loop_schedules() -> list[tuple[str, dt.datetime | None, int]]:
    """Return ``(loop_name, next_fire_at, cadence_seconds)`` per enabled mini-loop.

    ``next_fire_at`` is the cadence-ledger ``last_fired_at`` plus the loop's
    resolved cadence; ``None`` when the loop has never fired (no marker row) —
    the statusline renders that as ``due``. ``cadence_seconds`` is that resolved
    cadence — the denominator the statusline colors each chunk's imminence
    against. Disabled loops are omitted, and the snapshot already returns
    mini-loops sorted by name for a deterministic render.
    """
    return [
        (entry.name, entry.next_fire_at, entry.cadence_seconds) for entry in build_report().mini_loops if entry.enabled
    ]


__all__ = ["mini_loop_schedules"]
