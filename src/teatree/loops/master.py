"""Master tick fan-out — dispatch each row via its OWN load-bearing column (#1796, #2513, #2584).

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

**The ``script``/``prompt`` column is LOAD-BEARING (#2513 regression fix).** The
master no longer selects an admitted row's behaviour by a name-only registry
lookup (the regression that left the DB ``script`` column dead). For each admitted
row it READS the column: a **script** row's ``script`` is resolved to the loop's
OWN name (:func:`teatree.loops.run.script_path_to_loop_name`) and THAT loop's
``build_jobs`` fans out — a row whose ``script`` does not resolve to a real
registered loop module raises and is logged + skipped (never a silent no-op); a
**prompt** row dispatches its own loop's ``build_jobs`` (its scanner queues the
prompt-instructed work). So which behaviour fans out is decided by the row's
column, not by its name.

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
from typing import TYPE_CHECKING

from teatree.loop.job_identity import _ScannerJob
from teatree.loops.base import BuildJobsContext, MiniLoop
from teatree.loops.registry import iter_loops

if TYPE_CHECKING:
    from teatree.core.models import Loop

logger = logging.getLogger(__name__)


def _resolve_dispatch_loop(row: "Loop", registry_by_name: dict[str, MiniLoop]) -> MiniLoop:
    """The mini-loop an admitted ``row`` dispatches — decided by its column, not its name.

    A **script** row's ``Loop.script`` is parsed UP to the loop's own name
    (:func:`teatree.loops.run.parse_script_loop_name`) and that mini-loop is
    looked up in the per-tick registry; a stale/shared ``script`` (not the
    per-loop module shape) raises
    :class:`teatree.loops.run.UnresolvableScriptError` LOUDLY, and a name with no
    registry entry raises ``KeyError`` — both surface as a loud failure the master
    logs and skips, never a silent no-op. A **prompt** row dispatches its own
    registered mini-loop.
    """
    from teatree.loops.run import parse_script_loop_name  # noqa: PLC0415

    target = parse_script_loop_name(row.script) if row.script else row.name
    return registry_by_name[target]


def build_loop_table_jobs(scanner_context: BuildJobsContext, *, now: dt.datetime) -> list[_ScannerJob]:
    """Scanner jobs for every loop the unified verdict admits and whose cadence is due.

    An ``off_live_tick`` loop (the heavy ``dream`` pass, #1933 § 3) is skipped
    first, before any DB work — the live tick must never invoke its ``build_jobs``
    or bump its ``last_run_at``. A registry mini-loop with no ``Loop`` row is
    skipped (its config was never seeded). A loop whose row is disabled or
    not-due is skipped, AND a loop the unified :meth:`LoopsConfig.is_enabled`
    verdict holds — a ``LoopState`` PAUSED/DISABLED row or the
    ``T3_LOOPS_DISABLED`` env kill-switch (#1913, #2584) — is skipped too, BEFORE
    ``mark_run``, so a held loop's cadence anchor is preserved.

    For each admitted row the dispatch target is read from the row's OWN
    ``script``/``prompt`` column (#2513): a script row's ``script`` resolves to
    the loop it names, a prompt row dispatches its own loop. A row whose
    ``script`` does not resolve to a real registered loop module raises — that one
    loop is logged and skipped (never aborts the master tick, never a silent
    no-op) and its cadence anchor is NOT bumped. ``mark_run`` bumps the row's
    cadence anchor for each successfully-dispatched loop.
    """
    from teatree.core.models import Loop  # noqa: PLC0415
    from teatree.loops.config import LoopsConfig  # noqa: PLC0415

    config = LoopsConfig.load()
    registry = tuple(iter_loops())
    registry_by_name = {loop.name: loop for loop in registry}
    rows = {row.name: row for row in Loop.objects.all()}
    jobs: list[_ScannerJob] = []
    for loop in registry:
        if loop.off_live_tick:
            continue
        row = rows.get(loop.name)
        if row is None or not row.enabled or not row.is_due(now):
            continue
        if not config.is_enabled(loop):
            continue
        try:
            target = _resolve_dispatch_loop(row, registry_by_name)
            built = target.build_jobs(**scanner_context)
        except Exception:
            logger.exception("Loop %r raised while resolving/building jobs from its column — skipping", loop.name)
            continue
        jobs.extend(built)
        Loop.objects.mark_run(loop.name, now)
    return jobs
