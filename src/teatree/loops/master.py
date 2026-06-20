"""Master tick fan-out — gate the registry on the unified loop verdict (#1796, #2584).

The cutover from the fat code-cadence tick: the master no longer asks
``MiniLoopMarker`` whether a mini-loop should fire on its code cadence — the DB
``Loop`` row carries cadence + the enable toggle. #2584 closes the gap the
#2513 cutover opened: a loop runs this tick iff it is NOT ``off_live_tick`` AND
its ``Loop`` row is ``enabled`` and ``is_due(now)`` (its own ``delay_seconds``
interval, or its ``daily_at`` wall-clock schedule) AND ``LoopsConfig.is_enabled``
agrees. ``LoopsConfig.is_enabled`` composes the durable ``LoopState`` control
tier (``t3 loop pause`` / ``disable``, #1913), the ``T3_LOOPS_DISABLED`` env
kill-switch (respecting ``always_on`` only via its flag), and the per-loop /
global ``[loops]`` toml. Routing the master through it makes the live tick, the
orchestrator / scoped path, and the review-claim chokepoint reach the SAME
verdict for a given loop name — the levers the cutover dropped now bind here.

An ``off_live_tick`` loop (the heavy ``dream`` consolidation pass, #1933 § 3) is
NEVER picked up here — the live tick must not invoke its ``build_jobs`` or bump
its ``last_run_at``; it is driven by its own low-frequency cron. The
``LoopsConfig.is_enabled`` check runs BEFORE ``build_jobs`` / ``mark_run`` so a
held loop is neither dispatched nor cadence-bumped — its anchor is preserved,
not silently consumed. After building an admitted loop's jobs the master bumps
that row's ``last_run_at`` so the next tick's cadence gate sees the move.

This is the ``jobs_builder`` the master tick (``t3 loops tick``) injects into the
shared :func:`teatree.loop.tick.run_tick` pipeline, so reap + scan + act + render
are reused unchanged — only the gate (which loops run, on whose cadence) moves
from code into the DB rows + the unified verdict.
"""

import datetime as dt
import logging

from teatree.loop.job_identity import _ScannerJob
from teatree.loops.base import BuildJobsContext
from teatree.loops.registry import iter_loops

logger = logging.getLogger(__name__)


def build_loop_table_jobs(scanner_context: BuildJobsContext, *, now: dt.datetime) -> list[_ScannerJob]:
    """Scanner jobs for every loop the unified verdict admits and whose cadence is due.

    An ``off_live_tick`` loop (the heavy ``dream`` pass, #1933 § 3) is skipped
    first, before any DB lookup — the live tick must never invoke its
    ``build_jobs`` or bump its ``last_run_at``. A registry mini-loop with no
    ``Loop`` row is skipped (its config was never seeded). A loop whose row is
    disabled or not-due is skipped, AND a loop the unified
    :meth:`LoopsConfig.is_enabled` verdict holds — a ``LoopState`` PAUSED/DISABLED
    row or the ``T3_LOOPS_DISABLED`` env kill-switch (#1913, #2584) — is skipped
    too, BEFORE ``mark_run``, so a held loop's cadence anchor is preserved. One
    loop's ``build_jobs`` raising is logged and skipped — never aborts the master
    tick. ``mark_run`` bumps the row's cadence anchor for each admitted loop.
    """
    from teatree.core.models import Loop  # noqa: PLC0415
    from teatree.loops.config import LoopsConfig  # noqa: PLC0415

    config = LoopsConfig.load()
    rows = {row.name: row for row in Loop.objects.all()}
    jobs: list[_ScannerJob] = []
    for loop in iter_loops():
        if loop.off_live_tick:
            continue
        row = rows.get(loop.name)
        if row is None or not row.enabled or not row.is_due(now):
            continue
        if not config.is_enabled(loop):
            continue
        try:
            jobs.extend(loop.build_jobs(**scanner_context))
        except Exception:
            logger.exception("Loop %r raised during build_jobs — skipping", loop.name)
            continue
        Loop.objects.mark_run(loop.name, now)
    return jobs
