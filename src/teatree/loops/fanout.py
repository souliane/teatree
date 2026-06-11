"""Registry-driven scanner-job fan-out for the live tick (#1481).

The live tick reaches the mini-loop registry through here so the
registry is the single source of which scanners run a tick. Each
:class:`teatree.loops.base.MiniLoop` is gated by
:func:`teatree.loops.gating.elapsed_and_enabled` — the same decision
:class:`teatree.loops.orchestrator.Orchestrator` uses — then its
``build_jobs`` output is collected and its cadence marker bumped.

An ``off_live_tick`` loop (the heavy ``dream`` consolidation pass, #1933)
is skipped here: it is driven by its own low-frequency cron (``t3 dream
tick``) so it never runs on or re-arms the live 12-minute loop.

This module lives in :mod:`teatree.loops` (which may depend on
:mod:`teatree.loop`) so the dependency points up-stack: the live
``run_tick`` body in :mod:`teatree.loop` reaches it through the
``jobs_builder`` seam wired by the ``loop_tick`` management command,
never by importing the registry down into :mod:`teatree.loop`.
"""

import datetime as dt

from teatree.loop.job_identity import _ScannerJob
from teatree.loops.base import BuildJobsContext
from teatree.loops.cadence_ledger import MiniLoopMarker
from teatree.loops.config import LoopsConfig
from teatree.loops.gating import elapsed_and_enabled
from teatree.loops.registry import iter_loops


def build_registry_jobs(
    scanner_context: BuildJobsContext, *, config: LoopsConfig, now: dt.datetime
) -> list[_ScannerJob]:
    jobs: list[_ScannerJob] = []
    for loop in iter_loops():
        if loop.off_live_tick:
            continue
        if not elapsed_and_enabled(config, loop, now).should_fire:
            continue
        jobs.extend(loop.build_jobs(**scanner_context))
        MiniLoopMarker.objects.mark_fired(loop.name, now)
    return jobs
