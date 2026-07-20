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
the front-end drain loop). A :func:`drain_headless_queue` chain keeps the headless
backlog draining and re-enqueues runs a dead worker abandoned. A tight-cadence
:func:`run_slack_answer` chain drives the reactive Slack-answer cycle headless (the
👀-receipt + reply/delegate machinery that only ran in an interactive owner
session's ``/loop`` slot before), guarded by the SAME ``loop-slack-answer``
:class:`LoopLease` the ``loop_slack_answer`` mgmt command takes so the worker and an
interactive owner session can never double-post. A :func:`render_statusline` chain
(:mod:`teatree.loops.statusline_refresh`) keeps ``statusline.txt`` fresh on a short
cadence even when no domain loop is admitted-and-ticking, so the pre-rendered loop line
never freezes headless. The maintenance chains are seeded by
:func:`ensure_maintenance_chains` at worker startup and self-perpetuate, so a worker
restart re-arms them.
"""

import datetime as dt
import logging
import os

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
#: The headless-queue drain + stuck-run reaper cadence — the safety net that
#: keeps the headless backlog draining (``drain_headless_queue`` was previously
#: never scheduled from anywhere) and re-enqueues runs a dead worker abandoned.
DRAIN_INTERVAL_SECONDS = 300
#: A live headless run renews its ``Task`` lease from the heartbeat thread every
#: few seconds; the default claim lease is 300s. A RUNNING ``execute_headless_task``
#: whose ``Task`` lease has lapsed past this window has a dead worker — its
#: heartbeat stopped — so the ``DBTaskResult`` is stranded and must be reaped.
HEADLESS_LEASE_SECONDS = 300
#: The machine-wide lease name the reactive Slack-answer cycle runs under — the
#: SAME slot the ``loop_slack_answer`` mgmt command / interactive ``/loop`` slot
#: acquires, so the headless worker can never double-post against an owner session.
SLACK_ANSWER_LEASE = "loop-slack-answer"


def timer_chain_loop_names() -> set[str]:
    """The loops that should carry a timer chain: enabled, registered, and live-tick.

    Enabled ``Loop`` rows (the row-level ``enabled`` column) intersected with the
    registered mini-loops that are NOT ``off_live_tick`` — the heavy off-tick loops
    (``dream``) are driven by their own low-frequency cron, never a worker timer, so
    they never get a chain that would only ever no-op.
    """
    from teatree.core.models import Loop  # noqa: PLC0415 — deferred: ORM import needs the app registry
    from teatree.loops.registry import iter_loops  # noqa: PLC0415 — deferred: loaded at tick time, not import

    registered = {loop.name for loop in iter_loops() if not loop.off_live_tick}
    enabled = set(Loop.objects.enabled().values_list("name", flat=True))
    return registered & enabled


def _loop_timers_by_name(status: str) -> dict[str, list]:
    from django_tasks_db.models import DBTaskResult  # noqa: PLC0415 — deferred: heavy/optional dep at call site

    from teatree.loops.timer_chains import _loop_timer_path  # noqa: PLC0415 — deferred: loaded at tick time, not import

    grouped: dict[str, list] = {}
    for row in DBTaskResult.objects.filter(task_path=_loop_timer_path(), status=status):
        args = row.args_kwargs.get("args") or []
        if args:
            grouped.setdefault(args[0], []).append(row)
    return grouped


def _is_stranded(result, loop_row, now: dt.datetime) -> bool:  # noqa: ANN001 — untyped by design: a duck-typed handle passed positionally
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
    from django_tasks.base import TaskResultStatus  # noqa: PLC0415 — deferred: heavy/optional dep at call site
    from django_tasks_db.models import DBTaskResult  # noqa: PLC0415 — deferred: heavy/optional dep at call site

    from teatree.core.models import Loop  # noqa: PLC0415 — deferred: ORM import needs the app registry

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
    from django_tasks.base import TaskResultStatus  # noqa: PLC0415 — deferred: heavy/optional dep at call site
    from django_tasks_db.models import DBTaskResult  # noqa: PLC0415 — deferred: heavy/optional dep at call site

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
    from django_tasks.base import TaskResultStatus  # noqa: PLC0415 — deferred: heavy/optional dep at call site
    from django_tasks_db.models import DBTaskResult  # noqa: PLC0415 — deferred: heavy/optional dep at call site

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


class _StuckHeadlessRunError(RuntimeError):
    """Recorded on a stranded ``execute_headless_task`` DBTaskResult reaped by the reconciler."""


def _headless_task_id(row) -> int | None:  # noqa: ANN001 — duck-typed DBTaskResult handle
    """The ``Task`` pk a ``execute_headless_task`` DBTaskResult carries as its first arg."""
    args = row.args_kwargs.get("args") or []
    if not args:
        return None
    first = args[0]
    return first if isinstance(first, int) else None


def _headless_run_is_dead(task, row, now: dt.datetime) -> bool:  # noqa: ANN001 — duck-typed handles
    """Whether a RUNNING ``execute_headless_task`` row is a dead-worker orphan.

    The per-run liveness signal is the ``Task`` lease: the headless runner's
    heartbeat thread renews it every few seconds, so a live run always keeps
    ``lease_expires_at`` in the future. A run is dead when its heartbeat has
    stopped — the lease is absent (the claim was lease-reclaimed back to PENDING)
    or lapsed into the past. The ``started_at`` floor rules out the brief window
    between the row going RUNNING and the worker claiming + setting the lease, so
    a just-started healthy run is never reaped. A vanished ``Task`` row leaves an
    orphaned DBTaskResult that is likewise dead (and un-re-enqueueable).
    """
    from teatree.core.models import Task  # noqa: PLC0415 — deferred: ORM import needs the app registry

    if row.started_at is None:
        return False
    if row.started_at > now - dt.timedelta(seconds=HEADLESS_LEASE_SECONDS + STUCK_GRACE_SECONDS):
        return False
    if task is None:
        return True
    lease = task.lease_expires_at
    heartbeat_live = task.status == Task.Status.CLAIMED and lease is not None and lease > now
    return not heartbeat_live


def reap_stuck_headless_runs() -> dict[str, int]:
    """Fail dead-worker ``execute_headless_task`` runs and re-enqueue their live tasks (#10).

    ``timer_reconciler`` recovers only stranded ``loop_timer`` rows, and
    ``expire_stale_ready_jobs`` touches only READY rows — so a ``DBTaskResult``
    left RUNNING when a worker died mid-run wedges forever: the Task's lease is
    reclaimed back to PENDING but ``execute_headless_task``'s auto-enqueue fires
    only on post_save creation, so it is never re-run. This reaper closes that
    gap: each RUNNING ``execute_headless_task`` past its lease+grace with a dead
    heartbeat is marked FAILED (reversible, inspectable — no hard delete), and
    when its ``Task`` row is still non-terminal a fresh ``execute_headless_task``
    is enqueued so the work resumes. The claim CAS in ``execute_headless_task``
    makes a redundant re-enqueue safe (a second run loses the claim and fails
    cleanly, never double-executes).
    """
    from django_tasks.base import TaskResultStatus  # noqa: PLC0415 — deferred: heavy/optional dep at call site
    from django_tasks_db.models import DBTaskResult  # noqa: PLC0415 — deferred: heavy/optional dep at call site

    from teatree.core.models import Task  # noqa: PLC0415 — deferred: ORM import needs the app registry
    from teatree.core.tasks import execute_headless_task  # noqa: PLC0415 — deferred: task-body import

    now = timezone.now()
    running = DBTaskResult.objects.filter(
        task_path=execute_headless_task.module_path,
        status=TaskResultStatus.RUNNING,
    )
    counts = {"failed": 0, "reenqueued": 0}
    for row in running:
        task_id = _headless_task_id(row)
        if task_id is None:
            continue  # a malformed row with no identifiable Task — leave it untouched.
        task = Task.objects.filter(pk=task_id).first()
        if not _headless_run_is_dead(task, row, now):
            continue
        row.set_failed(_StuckHeadlessRunError(f"execute_headless_task {row.id} RUNNING past lease+grace; worker dead"))
        counts["failed"] += 1
        if task is not None and task.status not in Task.Status.terminal():
            execute_headless_task.enqueue(task.pk, task.phase)
            counts["reenqueued"] += 1
    if any(counts.values()):
        logger.info("reap_stuck_headless_runs: %s", counts)
    return counts


@task(queue_name=LOOPS_QUEUE)
def drain_headless_chain() -> dict[str, int]:
    """Reap dead headless runs, drain the pending headless backlog, re-schedule ~5min out.

    The scheduled home of ``drain_headless_queue`` — it was defined but NEVER
    scheduled from anywhere, so the pending headless backlog only drained on the
    post_save auto-enqueue (missed on a lease-reclaim / stale interactive row).
    Seeded by :func:`ensure_maintenance_chains` at worker startup and
    self-perpetuating, like its sibling reconcile/prune/expire chains. Runs on
    the ``loops`` queue and enqueues onto ``default`` (it never runs the heavy
    headless work itself). Self-dedups first so an at-least-once redelivery
    collapses to one.
    """
    from teatree.core.tasks import drain_headless_queue_body  # noqa: PLC0415 — deferred: task-body import

    if _pending_for_path(drain_headless_chain.module_path):
        return {"deduped": 1}
    reaped = reap_stuck_headless_runs()
    drained = drain_headless_queue_body()
    drain_headless_chain.using(run_after=timezone.now() + dt.timedelta(seconds=DRAIN_INTERVAL_SECONDS)).enqueue()
    return {
        "reaped_failed": reaped["failed"],
        "reaped_reenqueued": reaped["reenqueued"],
        "drained": len(drained["enqueued"]),
        "rerouted": len(drained["rerouted"]),
    }


@task(queue_name=LOOPS_QUEUE)
def run_slack_answer() -> dict[str, int]:
    """Run one reactive Slack-answer cycle headless, then re-schedule at its cadence.

    Self-dedups first (another pending run carries the chain), mirroring the
    reconcile/prune/expire contract, so an at-least-once redelivery collapses to
    one. Acquires the SAME ``loop-slack-answer`` :class:`LoopLease` the mgmt
    command / interactive ``/loop`` slot takes: if the lease is held (an owner
    session is already running the cycle) this tick SKIPS the cycle rather than
    double-post, but still re-arms the chain. On a win it runs
    :func:`run_slack_answer_cycle`, releases the lease in a ``finally``, and
    re-schedules itself at :func:`slack_answer_cadence_seconds`.
    """
    from teatree.core.models import LoopLease  # noqa: PLC0415 — deferred: ORM import needs the app registry
    from teatree.loop.loop_cadences import slack_answer_cadence_seconds  # noqa: PLC0415 — deferred: tick-time import
    from teatree.loop.slack_answer.cycle import run_slack_answer_cycle  # noqa: PLC0415 — deferred: task-body import

    if _pending_for_path(run_slack_answer.module_path):
        return {"deduped": 1}

    owner = f"worker-{os.getpid()}"
    if not LoopLease.objects.acquire(SLACK_ANSWER_LEASE, owner=owner):
        # An interactive owner session holds the slot — skip this tick's cycle so
        # the two can never double-post, but still re-arm the chain below.
        result: dict[str, int] = {"skipped_lease_held": 1}
    else:
        try:
            report = run_slack_answer_cycle()
            result = {
                "processed": report.processed,
                "eyes_reacted": report.eyes_reacted,
                "acked": report.acked,
                "answered_simple": report.answered_simple,
                "delegated": report.delegated,
                "errors": report.errors,
            }
        finally:
            LoopLease.objects.release(SLACK_ANSWER_LEASE, owner=owner)

    run_slack_answer.using(
        run_after=timezone.now() + dt.timedelta(seconds=slack_answer_cadence_seconds()),
    ).enqueue()
    return result


def ensure_maintenance_chains() -> None:
    """Seed reconcile / prune / expire / drain / slack-answer / usage-window / preset / statusline chains if absent."""
    from teatree.loop.loop_cadences import slack_answer_cadence_seconds  # noqa: PLC0415 — deferred: tick-time import
    from teatree.loops.preset_transitions import ensure_preset_transitions_chain  # noqa: PLC0415 — cycle-safe
    from teatree.loops.statusline_refresh import ensure_statusline_refresh_chain  # noqa: PLC0415 — cycle-safe
    from teatree.loops.usage_window_recovery import ensure_usage_window_recovery_chain  # noqa: PLC0415 — cycle-safe

    now = timezone.now()
    if not _pending_for_path(reconcile_timers.module_path):
        reconcile_timers.using(run_after=now + dt.timedelta(seconds=RECONCILE_INTERVAL_SECONDS)).enqueue()
    if not _pending_for_path(prune_task_results.module_path):
        prune_task_results.using(run_after=now + dt.timedelta(seconds=PRUNE_INTERVAL_SECONDS)).enqueue()
    if not _pending_for_path(expire_stale_jobs.module_path):
        expire_stale_jobs.using(run_after=now + dt.timedelta(seconds=EXPIRE_INTERVAL_SECONDS)).enqueue()
    # #10: the headless-queue drain + dead-run reaper. ``drain_headless_queue``
    # had zero call sites, so the pending headless backlog only drained on the
    # post_save auto-enqueue — a lease-reclaimed or stale-interactive row was
    # never re-dispatched. Seeding it here is the "actually run the drain" fix.
    if not _pending_for_path(drain_headless_chain.module_path):
        drain_headless_chain.using(run_after=now + dt.timedelta(seconds=DRAIN_INTERVAL_SECONDS)).enqueue()
    # The reactive Slack-answer cycle, armed headless so the worker drains the
    # 👀-receipt + reply/delegate machinery that only ran in an interactive owner
    # session's ``/loop`` slot before. Lease-guarded against the owner session.
    if not _pending_for_path(run_slack_answer.module_path):
        run_slack_answer.using(run_after=now + dt.timedelta(seconds=slack_answer_cadence_seconds())).enqueue()
    # Directive #3: the self-rescheduling usage-window re-arm chain. Its body is inert while
    # ``limit_autorecovery_enabled`` is OFF, so seeding it unconditionally is dark-safe.
    ensure_usage_window_recovery_chain()
    # #3159: the preset-transition side-effect chain (override reap, availability pin,
    # one Slack line per switch). Inert with no active preset — a cheap keepalive.
    ensure_preset_transitions_chain()
    # The headless statusline-render chain — keeps ``statusline.txt`` fresh on a short
    # cadence even when NO domain loop is admitted-and-ticking (the true cause of the
    # long-standing stale-loop-line complaint), gated by the ``autoload`` #256 flag.
    ensure_statusline_refresh_chain()
