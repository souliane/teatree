"""Master tick fan-out — gate the registry on the DB ``Loop`` table (#1796).

The cutover from the fat code-cadence tick: the master no longer asks
``LoopsConfig``/``MiniLoopMarker`` whether a mini-loop should fire — the DB
``Loop`` row is the single source of truth. A loop runs this tick iff it is NOT
``off_live_tick`` and its row is ``enabled`` and ``is_due(now)`` (its own
``delay_seconds`` interval, or its ``daily_at`` wall-clock schedule). An
``off_live_tick`` loop (the heavy ``dream`` consolidation pass, #1933 § 3) is
NEVER picked up here — the live tick must not invoke its ``build_jobs`` or bump
its ``last_run_at``; it is driven by its own low-frequency cron. After building a
loop's jobs the master bumps that row's ``last_run_at`` so the next tick's
cadence gate sees the move.

This is the ``jobs_builder`` the master tick (``t3 loops tick``) injects into the
shared :func:`teatree.loop.tick.run_tick` pipeline, so reap + scan + act + render
are reused unchanged — only the gate (which loops run, on whose cadence) moves
from code into the DB rows.
"""

import datetime as dt
import logging

from teatree.loop.job_identity import _ScannerJob
from teatree.loops.base import BuildJobsContext
from teatree.loops.registry import iter_loops

logger = logging.getLogger(__name__)


def build_loop_table_jobs(scanner_context: BuildJobsContext, *, now: dt.datetime) -> list[_ScannerJob]:
    """Scanner jobs for every enabled ``Loop`` row whose cadence is due at *now*.

    An ``off_live_tick`` loop (the heavy ``dream`` pass, #1933 § 3) is skipped
    first, before any DB lookup — the live tick must never invoke its
    ``build_jobs`` or bump its ``last_run_at``. A registry mini-loop with no
    ``Loop`` row is skipped (its config was never seeded). One loop's
    ``build_jobs`` raising is logged and skipped — never aborts the master tick.
    ``mark_run`` bumps the row's cadence anchor for each loop included.
    """
    from teatree.core.models import Loop  # noqa: PLC0415

    rows = {row.name: row for row in Loop.objects.all()}
    jobs: list[_ScannerJob] = []
    for loop in iter_loops():
        if loop.off_live_tick:
            continue
        row = rows.get(loop.name)
        if row is None or not row.enabled or not row.is_due(now):
            continue
        try:
            jobs.extend(loop.build_jobs(**scanner_context))
        except Exception:
            logger.exception("Loop %r raised during build_jobs — skipping", loop.name)
            continue
        Loop.objects.mark_run(loop.name, now)
    return jobs
