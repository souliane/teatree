"""The self-rescheduling statusline-render chain — headless freshness keeper (#256 stale-line).

The statusline file (``statusline.txt``) is written as a SIDE EFFECT of a per-loop
tick's render phase: ``t3 worker`` fires ``loops_tick --loop <name>`` for each enabled,
due :class:`~teatree.core.models.Loop` row, and only that path calls
:func:`teatree.loop.phases.render.render_phase`, which writes the file. So whenever no
domain loop is admitted-and-ticking — every enabled loop not-yet-due, held under a
preset, paused by availability, deduped, or lease-contended — the loop-timer chain keeps
firing but every fire takes a skip branch, the render phase never runs, and the
pre-rendered file FREEZES. The shell hook (``hooks/scripts/statusline.sh``) and
``t3 loop status`` both read that frozen file verbatim, so the owner sees a confident,
hours-old loop line ("next tick 4m" that will never come). That coupling is the true
cause of the long-standing "stale statusline" complaint.

This module decouples the render from domain-loop admission. A single self-rescheduling
``render_statusline`` job on the shared :data:`~teatree.loops.timer_chains.LOOPS_QUEUE`
(the #1796 machinery, NO OS cron) polls on a short cadence and, whenever the rendered
file has aged past :data:`REFRESH_AGE_SECONDS`, re-renders it from live state via the
idle-render seam :func:`teatree.loop.phases.render.rerender_statusline` and refreshes the
``tick-meta.json`` freshness sidecar. It is the deterministic, zero-token, zero-inference
twin of the ``reconcile_timers`` / ``usage_window_recovery`` maintenance chains: the
refresh decision is purely the file's own render age, so a healthy fleet whose per-loop
ticks already keep the file fresh leaves this chain dormant (each fire sees a young file
and does nothing — no flicker, no interference), while a quiet fleet has its loop line
kept live within ~a minute regardless.

Gated by the ``autoload`` owner flag — the SAME #256/#3273 visibility gate the shell hook
consults — so a colleague box that merely cloned the repo never has its statusline
rendered here (the #256 colleague guarantee: ``autoload`` off ⇒ no loop statusline). While
autoload is OFF the chain is a cheap keepalive that renders nothing but keeps
re-scheduling, so a flag flip is picked up without a worker restart. Seeded by
:func:`teatree.loops.timer_reconciler.ensure_maintenance_chains` at worker startup and
self-perpetuating after that, so a worker restart / deploy re-arms it.
"""

import datetime as dt
import logging
import os

from django.tasks import task
from django.utils import timezone

from teatree.loops.timer_chains import LOOPS_QUEUE

logger = logging.getLogger(__name__)

#: The chain re-arms on this cadence — the poll interval at which the render age is
#: re-checked. Short so a frozen file is caught within ~a poll of crossing the refresh
#: age, well under any stale cutoff.
RENDER_POLL_SECONDS = 30

#: Re-render once the file's last render is at least this old. Kept comfortably below the
#: stale-banner floor (:data:`teatree.loop.statusline_staleness.FLOOR_SECONDS`, 300s) so
#: the file never reaches the "STALE" threshold while the worker is alive, yet above the
#: shortest default per-loop cadence (60s) so a healthy fleet's own ticks keep the file
#: young enough that this chain stays dormant and never blanks their scanned zones.
REFRESH_AGE_SECONDS = 90

#: The machine-wide lease serialising the render so two at-least-once redeliveries (or two
#: ``loops`` executor threads) can never both install the process-global loop-line reader
#: seams and race their teardown. A losing fire skips the render but still re-arms.
STATUSLINE_RENDER_LEASE = "loop-statusline-render"


def _autoload_enabled() -> bool:
    """Whether the ``autoload`` owner flag resolves ON — fail-safe OFF (the #256 gate).

    The same flag the shell hook and loop-arming consult: OFF means a colleague box, so
    the statusline must not be rendered here. A settings-read failure degrades to OFF so a
    broken config read can never render a loop statusline onto a box that never opted in.
    """
    try:
        from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred: task-body import

        return bool(get_effective_settings().autoload)
    except Exception:
        logger.debug("autoload read failed — treating the statusline-refresh chain as gated OFF", exc_info=True)
        return False


def _pending_render() -> bool:
    from django_tasks.base import TaskResultStatus  # noqa: PLC0415 — deferred: heavy/optional dep at call site
    from django_tasks_db.models import DBTaskResult  # noqa: PLC0415 — deferred: heavy/optional dep at call site

    return DBTaskResult.objects.filter(
        task_path=render_statusline.module_path,
        status=TaskResultStatus.READY,
    ).exists()


def _render_is_due(now: dt.datetime) -> bool:
    """Whether the rendered statusline has aged past :data:`REFRESH_AGE_SECONDS`.

    Reads the render age from the SAME ``tick-meta.json`` source the stale-banner probes
    use (:func:`teatree.loop.statusline_staleness.render_age_seconds`), so the freshness
    math can never drift between the writer keeping the file fresh and the readers flagging
    it stale. A never-rendered file (no sidecar / unknown age) is due — the chain
    bootstraps it. A fresh file (a recent per-loop tick already rendered it) is NOT due, so
    this chain stays dormant and does not blank that tick's scanned zones.
    """
    from teatree.loop.statusline import default_path  # noqa: PLC0415 — deferred: task-body import
    from teatree.loop.statusline_staleness import render_age_seconds  # noqa: PLC0415 — deferred: task-body import

    age = render_age_seconds(default_path(), now=now.timestamp())
    return age is None or age >= REFRESH_AGE_SECONDS


def _refresh_statusline(now: dt.datetime) -> None:
    """Re-render the statusline from live state and refresh the freshness sidecar.

    Installs the live DB-backed loop-line readers (mini-loop schedules, active-preset
    handle, overridden loops) exactly as ``loops_tick`` does so the rendered loop line
    carries its per-loop countdowns and preset handle, runs the idle (no-scan)
    :func:`rerender_statusline` — cheap, zero-token, and reading the existing open-PR
    snapshot rather than blanking it — then writes ``tick-meta.json`` so the ``rendered_at``
    freshness epoch the stale-banner probes read advances in lock-step with the file. The
    process-global reader seams are always reset in ``finally`` so they never leak past
    this render.
    """
    from teatree.loop.phases.render import rerender_statusline  # noqa: PLC0415 — deferred: task-body import
    from teatree.loop.statusline import (  # noqa: PLC0415 — deferred: task-body import
        set_mini_loop_schedules_reader,
        set_overridden_loops_reader,
        set_preset_line_reader,
    )
    from teatree.loop.tick import _write_tick_meta  # noqa: PLC0415 — deferred: task-body import
    from teatree.loops.preset_status import overridden_loop_names, preset_line_handles  # noqa: PLC0415 — deferred
    from teatree.loops.schedule import mini_loop_schedules  # noqa: PLC0415 — deferred: task-body import

    set_mini_loop_schedules_reader(mini_loop_schedules)
    set_preset_line_reader(preset_line_handles)
    set_overridden_loops_reader(overridden_loop_names)
    try:
        rerender_statusline()
        _write_tick_meta(now)
    finally:
        set_mini_loop_schedules_reader(None)
        set_preset_line_reader(None)
        set_overridden_loops_reader(None)


def refresh_statusline_if_due(now: dt.datetime) -> str:
    """Render the statusline when it has aged past the refresh threshold; return the outcome.

    The pure body of one chain fire (no re-scheduling), split out so a test drives the
    decision without the queue. Returns ``"gated"`` (autoload OFF — the #256 colleague
    guarantee), ``"contended"`` (another render holds the lease), ``"fresh"`` (a recent
    render already keeps the file young — this chain stays dormant), or ``"rendered"``.
    Fully fail-open: any render error is swallowed so a broken render can never wedge the
    chain or crash the worker.
    """
    if not _autoload_enabled():
        return "gated"
    if not _render_is_due(now):
        return "fresh"

    from teatree.core.models import LoopLease  # noqa: PLC0415 — deferred: ORM import needs the app registry

    owner = f"worker-{os.getpid()}"
    if not LoopLease.objects.acquire(STATUSLINE_RENDER_LEASE, owner=owner):
        return "contended"
    try:
        _refresh_statusline(now)
    except Exception:
        logger.exception("statusline refresh render failed — the chain re-arms and retries next poll")
        return "fresh"
    finally:
        LoopLease.objects.release(STATUSLINE_RENDER_LEASE, owner=owner)
    return "rendered"


@task(queue_name=LOOPS_QUEUE)
def render_statusline() -> dict[str, str]:
    """One render-refresh fire: re-render the statusline if stale, then re-schedule the chain.

    Self-dedups first (another pending render carries the chain), mirroring the
    ``reconcile_timers`` contract, so an at-least-once redelivery collapses to one. The
    body is a no-op when autoload is OFF or the file is already fresh, but the chain always
    re-schedules itself :data:`RENDER_POLL_SECONDS` out so a flag flip or a fleet going
    quiet is picked up without a worker restart.
    """
    if _pending_render():
        return {"action": "deduped"}
    outcome = refresh_statusline_if_due(timezone.now())
    render_statusline.using(run_after=timezone.now() + dt.timedelta(seconds=RENDER_POLL_SECONDS)).enqueue()
    return {"action": outcome}


def ensure_statusline_refresh_chain() -> None:
    """Seed the render-refresh chain head if absent — self-perpetuating after (worker startup)."""
    if not _pending_render():
        render_statusline.using(run_after=timezone.now() + dt.timedelta(seconds=RENDER_POLL_SECONDS)).enqueue()
