"""Deterministic, zero-token reconciler for the loop-timer chains (#1796).

:func:`ensure_loop_timers` is the structural repair arm that keeps the "exactly
one pending ``loop_timer`` per enabled loop" invariant true against every way it
can drift: it adds a missing chain head, prunes a surplus timer, repairs a chain
stuck RUNNING past its deadline (a worker that died mid-tick), and deletes the
queued timers of a disabled or unknown loop. It dispatches no work and calls no
model — pure DB reconciliation — and it is idempotent, so re-running it on a
healthy set is a no-op.

It runs at three moments: at worker startup, on its own ~5-minute self-rescheduling
chain (:func:`reconcile_timers`), and from the loop enable/disable chokepoint so a
newly-enabled loop gets its head at once and a disabled one is pruned at once. A
daily :func:`prune_task_results` chain caps DBTaskResult table growth, and an hourly
:func:`expire_stale_jobs` chain keeps the ``default``-queue backlog swept for a
long-lived worker (so it never blind-fires days-old provision/ship jobs even without
the front-end drain loop). The three maintenance chains are seeded by
:func:`ensure_maintenance_chains` at worker startup and self-perpetuate, so a worker
restart re-arms them.
"""

import datetime as dt
import logging

from django.tasks import task
from django.utils import timezone

from teatree.loops.timer_chains import (
    LOOPS_QUEUE,
    compute_successor_run_after,
    compute_tick_deadline,
    enqueue_loop_timer,
)

logger = logging.getLogger(__name__)

#: The reconciler's own cadence — it re-runs every ~5 minutes off its own chain.
RECONCILE_INTERVAL_SECONDS = 300
#: The result-prune cadence and how long a finished result is kept before pruning.
PRUNE_INTERVAL_SECONDS = 86400
PRUNE_RETENTION_SECONDS = 86400
#: The stale-job expiry cadence — hourly, so a long-lived worker keeps the
#: ``default``-queue backlog swept without depending on the front-end drain loop.
EXPIRE_INTERVAL_SECONDS = 3600
#: Grace past a tick's deadline before its still-RUNNING timer is deemed stranded.
STUCK_GRACE_SECONDS = 60


def timer_chain_loop_names() -> set[str]:
    """The loops that should carry a timer chain: enabled, registered, and live-tick.

    Enabled ``Loop`` rows (the row-level ``enabled`` column) intersected with the
    registered mini-loops that are NOT ``off_live_tick`` — the heavy off-tick loops
    (``dream``) are driven by their own low-frequency cron, never a worker timer, so
    they never get a chain that would only ever no-op.
    """
    from teatree.core.models import Loop  # noqa: PLC0415
    from teatree.loops.registry import iter_loops  # noqa: PLC0415

    registered = {loop.name for loop in iter_loops() if not loop.off_live_tick}
    enabled = set(Loop.objects.enabled().values_list("name", flat=True))
    return registered & enabled


def _loop_timers_by_name(status: str) -> dict[str, list]:
    from django_tasks_db.models import DBTaskResult  # noqa: PLC0415

    from teatree.loops.timer_chains import _loop_timer_path  # noqa: PLC0415

    grouped: dict[str, list] = {}
    for row in DBTaskResult.objects.filter(task_path=_loop_timer_path(), status=status):
        args = row.args_kwargs.get("args") or []
        if args:
            grouped.setdefault(args[0], []).append(row)
    return grouped


def _is_stranded(result, loop_row, now: dt.datetime) -> bool:  # noqa: ANN001
    """Whether a RUNNING timer has outlived its tick deadline + grace (a dead worker)."""
    if result.started_at is None:
        return False
    limit = compute_tick_deadline(loop_row) + STUCK_GRACE_SECONDS
    return result.started_at < now - dt.timedelta(seconds=limit)


def ensure_loop_timers() -> dict[str, int]:
    """Reconcile the loop-timer chains to the enabled-loop set; return the repair counts.

    Deterministic and idempotent. Adds a head for an enabled loop with no live
    timer, prunes surplus queued timers (keeping the earliest), deletes a stranded
    RUNNING timer and re-heads its loop, and deletes the queued timers of a
    disabled/unknown loop. Dispatches nothing.
    """
    from django_tasks.base import TaskResultStatus  # noqa: PLC0415
    from django_tasks_db.models import DBTaskResult  # noqa: PLC0415

    from teatree.core.models import Loop  # noqa: PLC0415

    now = timezone.now()
    chain_names = timer_chain_loop_names()
    loops = {row.name: row for row in Loop.objects.filter(name__in=chain_names)}
    ready_by_name = _loop_timers_by_name(TaskResultStatus.READY)
    running_by_name = _loop_timers_by_name(TaskResultStatus.RUNNING)

    counts = {"added": 0, "pruned": 0, "repaired": 0}

    for name in chain_names:
        loop_row = loops[name]
        ready = sorted(ready_by_name.get(name, []), key=lambda r: r.run_after)
        running = running_by_name.get(name, [])
        stranded = [r for r in running if _is_stranded(r, loop_row, now)]
        live_running = [r for r in running if r not in stranded]

        for result in stranded:
            DBTaskResult.objects.filter(id=result.id).delete()
            counts["repaired"] += 1
        for surplus in ready[1:]:
            DBTaskResult.objects.filter(id=surplus.id).delete()
            counts["pruned"] += 1

        if not ready and not live_running:
            enqueue_loop_timer(name, run_after=compute_successor_run_after(loop_row, now))
            counts["added"] += 1

    # Disabled / unknown loops: prune their QUEUED fires (a RUNNING one dies on its
    # own next fire — admission fails or the row is gone — and is cleaned up then).
    for name, rows in ready_by_name.items():
        if name in chain_names:
            continue
        for result in rows:
            DBTaskResult.objects.filter(id=result.id).delete()
            counts["pruned"] += 1

    if any(counts.values()):
        logger.info("ensure_loop_timers: %s", counts)
    return counts


def _pending_for_path(path: str) -> bool:
    from django_tasks.base import TaskResultStatus  # noqa: PLC0415
    from django_tasks_db.models import DBTaskResult  # noqa: PLC0415

    return DBTaskResult.objects.filter(task_path=path, status=TaskResultStatus.READY).exists()


@task(queue_name=LOOPS_QUEUE)
def reconcile_timers() -> dict[str, int]:
    """Reconcile the chains, then re-schedule this reconciler ~5 minutes out.

    Self-dedups first (another pending reconciler carries the chain) so an
    at-least-once redelivery collapses to one, mirroring the loop-timer contract.
    """
    if _pending_for_path(reconcile_timers.module_path):
        return {"deduped": 1}
    counts = ensure_loop_timers()
    reconcile_timers.using(run_after=timezone.now() + dt.timedelta(seconds=RECONCILE_INTERVAL_SECONDS)).enqueue()
    return counts


@task(queue_name=LOOPS_QUEUE)
def prune_task_results() -> dict[str, int]:
    """Delete finished DBTaskResults older than the retention window, then re-schedule daily.

    Caps unbounded growth of the results table the timer chains churn. Only FINISHED
    (successful/failed) rows past the retention window are removed — a READY or
    RUNNING row is never touched.
    """
    from django_tasks.base import TaskResultStatus  # noqa: PLC0415
    from django_tasks_db.models import DBTaskResult  # noqa: PLC0415

    if _pending_for_path(prune_task_results.module_path):
        return {"deduped": 1}
    cutoff = timezone.now() - dt.timedelta(seconds=PRUNE_RETENTION_SECONDS)
    deleted, _ = (
        DBTaskResult.objects.filter(
            status__in=[TaskResultStatus.SUCCESSFUL, TaskResultStatus.FAILED],
            finished_at__lt=cutoff,
        )
        .exclude(finished_at=None)
        .delete()
    )
    prune_task_results.using(run_after=timezone.now() + dt.timedelta(seconds=PRUNE_INTERVAL_SECONDS)).enqueue()
    return {"pruned": deleted}


@task(queue_name=LOOPS_QUEUE)
def expire_stale_jobs() -> dict[str, int]:
    """Expire the stale ``default``-queue backlog, then re-schedule this chain ~1h out.

    Self-dedups first (another pending expiry carries the chain), mirroring the
    reconcile/prune contract, so an at-least-once redelivery collapses to one. Runs on
    the ``loops`` queue like its sibling maintenance chains — it never runs the heavy
    jobs, it only retires the stale READY ones to FAILED (reversible, auditable).
    """
    from teatree.loop.queue_drain import expire_stale_default_jobs  # noqa: PLC0415 — deferred: task-body import

    if _pending_for_path(expire_stale_jobs.module_path):
        return {"deduped": 1}
    retired = expire_stale_default_jobs()
    expire_stale_jobs.using(run_after=timezone.now() + dt.timedelta(seconds=EXPIRE_INTERVAL_SECONDS)).enqueue()
    return {"retired": sum(retired.values())}


def ensure_maintenance_chains() -> None:
    """Seed the reconcile + prune + stale-job-expiry + usage-window-recovery + preset chains if absent."""
    from teatree.loops.preset_transitions import ensure_preset_transitions_chain  # noqa: PLC0415 — cycle-safe
    from teatree.loops.usage_window_recovery import ensure_usage_window_recovery_chain  # noqa: PLC0415 — cycle-safe

    now = timezone.now()
    if not _pending_for_path(reconcile_timers.module_path):
        reconcile_timers.using(run_after=now + dt.timedelta(seconds=RECONCILE_INTERVAL_SECONDS)).enqueue()
    if not _pending_for_path(prune_task_results.module_path):
        prune_task_results.using(run_after=now + dt.timedelta(seconds=PRUNE_INTERVAL_SECONDS)).enqueue()
    if not _pending_for_path(expire_stale_jobs.module_path):
        expire_stale_jobs.using(run_after=now + dt.timedelta(seconds=EXPIRE_INTERVAL_SECONDS)).enqueue()
    # Directive #3: the self-rescheduling usage-window re-arm chain. Its body is inert while
    # ``limit_autorecovery_enabled`` is OFF, so seeding it unconditionally is dark-safe.
    ensure_usage_window_recovery_chain()
    # #3159: the preset-transition side-effect chain (override reap, availability pin,
    # one Slack line per switch). Inert with no active preset — a cheap keepalive.
    ensure_preset_transitions_chain()
