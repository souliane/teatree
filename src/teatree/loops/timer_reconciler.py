"""Deterministic, zero-token reconciler for the loop-timer chains (#1796).

:func:`ensure_loop_timers` is the structural repair arm that keeps the "exactly
one pending ``loop_timer`` per verdict-admitted loop" invariant true against every way it
can drift: it adds a missing chain head, prunes a surplus timer, repairs a chain
stuck RUNNING past its deadline (a worker that died mid-tick), and deletes the
queued timers of an un-admitted or unknown loop. It dispatches no work and calls no
model — pure DB reconciliation — and it is idempotent, so re-running it on a
healthy set is a no-op.

It runs at three moments: at worker startup, on its own ~5-minute self-rescheduling
chain (:func:`reconcile_timers`), and from the loop enable/disable chokepoint so a
newly-enabled loop gets its head at once and a disabled one is pruned at once. A
daily :func:`prune_task_results` chain caps DBTaskResult table growth, and an hourly
:func:`expire_stale_jobs` chain keeps the ``default``-queue backlog swept for a
long-lived worker (so it never blind-fires days-old provision/ship jobs even without
the front-end drain loop). A :func:`drain_queue` chain keeps the headless
backlog draining and re-enqueues runs a dead worker abandoned. A fallback-cadence
(5m default) :func:`run_slack_answer` chain drives the reactive Slack-answer cycle
headless (the 👀-receipt + reply/delegate machinery that only ran in an interactive
owner session's ``/loop`` slot before), guarded by the SAME ``loop-slack-answer``
:class:`LoopLease` the ``loop_slack_answer`` mgmt command takes so the worker and an
interactive owner session can never double-post. The same lease-guarded cycle is
also driven event-first: the Socket Mode receiver enqueues a one-shot
:func:`wake_slack_answer` the moment it appends an inbound event it did not
itself author (#4707), so a reply lands in ~one worker poll instead of waiting
out the cadence, while :func:`run_slack_answer` stays as the fallback that
drains anything a missed or debounced wake left behind. A :func:`render_statusline` chain
(:mod:`teatree.loops.statusline_refresh`) keeps ``statusline.txt`` fresh on a short
cadence even when no domain loop is admitted-and-ticking, so the pre-rendered loop line
never freezes headless. The :mod:`teatree.loops.off_live_tick_driver` chain fires each ``off_live_tick``
loop's own tick command (``directive`` / ``dream`` / ``outer``) as a deadlined
subprocess — those loops are excluded from BOTH the live fan-out and the timer chains,
so without it they have no driver at all. The maintenance chains are seeded by
:func:`ensure_maintenance_chains` at worker startup and self-perpetuate, so a worker
restart re-arms them.
"""

import datetime as dt
import io
import json
import logging
import os
import re

from django.core.management import call_command
from django.db import transaction
from django.tasks import task
from django.utils import timezone

from teatree.core.claim_liveness import ClaimOwner, owner_is_executing
from teatree.loops.chain_membership import loop_timers_by_name, timer_chain_loop_names
from teatree.loops.timer_chains import LOOPS_QUEUE, compute_successor_run_after, enqueue_loop_timer

logger = logging.getLogger(__name__)

#: The reconciler's own cadence — it re-runs every ~5 minutes off its own chain.
RECONCILE_INTERVAL_SECONDS = 300
#: The result-prune cadence. How long a finished result is kept is the
#: ``task_result_retention_days`` setting, not a constant here.
PRUNE_INTERVAL_SECONDS = 86400
#: The stale-job expiry cadence — hourly, so a long-lived worker keeps the
#: ``default``-queue backlog swept without depending on the front-end drain loop.
EXPIRE_INTERVAL_SECONDS = 3600
#: Floor on the spacing between two event-driven answer wakes. An inbound burst
#: collapses to one cycle plus one trailing catch-up instead of one cycle per
#: event, whatever the burst's source (#4707).
WAKE_MIN_INTERVAL_SECONDS = 10
#: Grace past a tick's deadline before its still-RUNNING timer is deemed stranded.
STUCK_GRACE_SECONDS = 60
#: The headless-queue drain + stuck-run reaper cadence — the safety net that
#: keeps the headless backlog draining (``drain_queue`` was previously
#: never scheduled from anywhere) and re-enqueues runs a dead worker abandoned.
DRAIN_INTERVAL_SECONDS = 300
#: A live headless run renews its ``Task`` lease from the heartbeat thread every
#: few seconds; the default claim lease is 300s. A RUNNING ``execute_task``
#: whose ``Task`` lease has lapsed past this window has a dead worker — its
#: heartbeat stopped — so the ``DBTaskResult`` is stranded and must be reaped.
HEADLESS_LEASE_SECONDS = 300
#: The machine-wide lease name the reactive Slack-answer cycle runs under — the
#: SAME slot the ``loop_slack_answer`` mgmt command / interactive ``/loop`` slot
#: acquires, so the headless worker can never double-post against an owner session.
SLACK_ANSWER_LEASE = "loop-slack-answer"
#: The machine-wide lease the self-improve cycle runs under — the SAME slot the
#: ``loop_self_improve`` mgmt command / interactive ``/loop`` slot acquires, so the
#: worker chain and an owner session can never run two cycles at once.
SELF_IMPROVE_LEASE = "loop-self-improve"
#: The only tier with detectors wired; the mgmt command refuses the rest.
SELF_IMPROVE_TIER = "cheap"
_SAFE_SCAN_LABEL = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


def ensure_loop_timers() -> dict[str, int]:
    """Reconcile the loop-timer chains to the ADMITTED loop set; return the repair counts.

    Deterministic and idempotent. Adds a head for an admitted loop with no live
    timer, prunes surplus queued timers (keeping the earliest), deletes a stranded
    RUNNING timer and re-heads its loop, and deletes the queued timers of an
    un-admitted/unknown loop. Dispatches nothing.

    The admitted set is :func:`timer_chain_loop_names`, so a preset-forced-ON loop is
    headed and a preset-masked-off or ``LoopState``-held one loses its chain rather than
    idle-polling at the cadence floor. Both directions are restored at their own
    chokepoints — the ``loop_state`` command and the dash loop control call this on
    resume, :func:`teatree.loops.preset_transitions.apply_preset_transition` calls it on a
    mode switch, and the 5-minute reconcile chain is the backstop.
    """
    from django.tasks import TaskResultStatus  # noqa: PLC0415 — deferred: Django import at call time
    from django_tasks_db.models import DBTaskResult  # noqa: PLC0415 — deferred: Django import at call time

    from teatree.core.models import Loop  # noqa: PLC0415 — deferred: ORM import needs the app registry
    from teatree.loops.schedule_liveness import (  # noqa: PLC0415 — deferred: breaks the liveness/reconciler import cycle
        is_stranded,
    )

    now = timezone.now()
    chain_names = timer_chain_loop_names()
    loops = {row.name: row for row in Loop.objects.filter(name__in=chain_names)}
    ready_by_name = loop_timers_by_name(TaskResultStatus.READY)
    running_by_name = loop_timers_by_name(TaskResultStatus.RUNNING)

    counts = {"added": 0, "pruned": 0, "repaired": 0}

    for name in chain_names:
        loop_row = loops[name]
        ready = sorted(ready_by_name.get(name, []), key=lambda r: r.run_after)
        running = running_by_name.get(name, [])
        stranded = [r for r in running if is_stranded(r, loop_row, now)]
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

    # Un-admitted / unknown loops: prune their QUEUED fires (a RUNNING one dies on its
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
    from django.tasks import TaskResultStatus  # noqa: PLC0415 — deferred: Django import at call time
    from django_tasks_db.models import DBTaskResult  # noqa: PLC0415 — deferred: Django import at call time

    return DBTaskResult.objects.filter(task_path=path, status=TaskResultStatus.READY).exists()


def _cycle_finished_within(path: str, seconds: int) -> dt.datetime | None:
    """When *path* last finished a real cycle, if that was under *seconds* ago — else ``None``.

    The window is in the filter rather than applied to a newest-first scan so
    the query stays bounded to the few rows the interval can hold.
    """
    from django_tasks_db.models import DBTaskResult  # noqa: PLC0415 — deferred: Django import at call time

    cutoff = timezone.now() - dt.timedelta(seconds=seconds)
    return (
        DBTaskResult.objects.filter(task_path=path, finished_at__gt=cutoff)
        .exclude(return_value__has_key="coalesced")
        .exclude(return_value__has_key="deduped")
        .order_by("-finished_at")
        .values_list("finished_at", flat=True)
        .first()
    )


@task(queue_name=LOOPS_QUEUE)
def reconcile_timers() -> dict[str, int]:
    """Re-schedule this reconciler ~5 minutes out, THEN reconcile the chains.

    Self-dedups first (another pending reconciler carries the chain) so an
    at-least-once redelivery collapses to one, mirroring the loop-timer contract.
    Successor-FIRST (F6): the next fire is queued BEFORE the body runs, so a body
    exception cannot orphan the chain. This is the repair chain for every OTHER
    chain, so orphaning it would strand the whole maintenance mesh until a worker
    restart — the body therefore runs in a try that records-but-never-propagates.
    """
    if _pending_for_path(reconcile_timers.module_path):
        return {"deduped": 1}
    reconcile_timers.using(run_after=timezone.now() + dt.timedelta(seconds=RECONCILE_INTERVAL_SECONDS)).enqueue()
    try:
        return ensure_loop_timers()
    except Exception:
        logger.exception("reconcile_timers body failed; successor already queued, the chain survives")
        return {"error": 1}


@task(queue_name=LOOPS_QUEUE)
def prune_task_results() -> dict[str, int]:
    """Re-schedule daily, THEN delete finished DBTaskResults older than the retention window.

    Caps unbounded growth of the results table the timer chains churn. The delete is
    :func:`teatree.core.retention.task_results.prune_finished_task_results` — the same
    seam ``t3 <overlay> retention prune`` uses, so the scheduled pass and the operator's
    pass cannot disagree about which rows are disposable, and neither hand-writes a
    prune over ``django_tasks_db``'s table. Only FINISHED (successful/failed) rows past
    the window go; a READY or RUNNING row is never touched. The window is the
    ``task_result_retention_days`` setting (a ``0`` disables the chain's delete, leaving
    the reconciler's own surplus/stranded pruning untouched). Successor-FIRST (F6): the
    next fire is queued before the delete runs, in a try that records-but-never-propagates,
    so a body fault cannot orphan the chain.
    """
    from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred: config read at call time
    from teatree.core.retention.task_results import prune_finished_task_results  # noqa: PLC0415 — deferred: heavy dep

    if _pending_for_path(prune_task_results.module_path):
        return {"deduped": 1}
    prune_task_results.using(run_after=timezone.now() + dt.timedelta(seconds=PRUNE_INTERVAL_SECONDS)).enqueue()
    try:
        days = int(get_effective_settings().task_result_retention_days)
        deleted = prune_finished_task_results(days=days) if days > 0 else 0
    except Exception:
        logger.exception("prune_task_results body failed; successor already queued, the chain survives")
        return {"error": 1}
    return {"pruned": deleted}


@task(queue_name=LOOPS_QUEUE)
def expire_stale_jobs() -> dict[str, int]:
    """Expire the stale ``default``-queue backlog, then re-schedule this chain ~1h out.

    Self-dedups first (another pending expiry carries the chain), mirroring the
    reconcile/prune contract, so an at-least-once redelivery collapses to one. Runs on
    the ``loops`` queue like its sibling maintenance chains — it never runs the heavy
    jobs, it only retires the stale READY ones to FAILED (reversible, auditable).
    """
    from teatree.loop.queue_drain import expire_stale_headless_jobs  # noqa: PLC0415 — deferred: task-body import

    if _pending_for_path(expire_stale_jobs.module_path):
        return {"deduped": 1}
    expire_stale_jobs.using(run_after=timezone.now() + dt.timedelta(seconds=EXPIRE_INTERVAL_SECONDS)).enqueue()
    try:
        retired = expire_stale_headless_jobs()
    except Exception:
        logger.exception("expire_stale_jobs body failed; successor already queued, the chain survives")
        return {"error": 1}
    return {"retired": sum(retired.values())}


class _StuckHeadlessRunError(RuntimeError):
    """Recorded on a stranded ``execute_task`` DBTaskResult reaped by the reconciler."""


def _headless_task_id(row) -> int | None:  # noqa: ANN001 — duck-typed DBTaskResult handle
    """The ``Task`` pk a ``execute_task`` DBTaskResult carries as its first arg."""
    args = row.args_kwargs.get("args") or []
    if not args:
        return None
    first = args[0]
    return first if isinstance(first, int) else None


def _headless_run_is_dead(task, row, now: dt.datetime) -> bool:  # noqa: ANN001 — duck-typed handles
    """Whether a RUNNING ``execute_task`` row is a dead-worker orphan.

    Two independent liveness signals, and the run is dead only when NEITHER holds.

    The first is the ``Task`` lease: the agent runner's heartbeat thread renews it every
    few seconds, so a live run keeps ``lease_expires_at`` in the future, and a stopped
    heartbeat leaves the lease absent (lease-reclaimed back to PENDING) or lapsed. The
    second is the owner PROCESS (#4164) — a lapsed lease is evidence about the lease
    alone, and under memory pressure the runner's event loop stalls past its 900s lease
    while the agent is still producing work. Reaping on the lease alone marked that row
    FAILED (which does not kill the process) and enqueued a SECOND ``execute_task``; the
    re-enqueued job's claim CAS treats an expired lease as claimable, so the duplicate won
    the claim and the ORIGINAL aborted ``LeaseLostError`` — one memory blip costing a run
    plus a full re-execution, with both agents briefly live on the same worktree.

    The ``started_at`` floor rules out the brief window between the row going RUNNING and
    the worker claiming + setting the lease, so a just-started healthy run is never reaped.
    A vanished ``Task`` row leaves an orphaned DBTaskResult that is likewise dead (and
    un-re-enqueueable).
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
    return not heartbeat_live and not owner_is_executing(ClaimOwner.of(task), task.pk, now=now)


def _dispatch_refusal() -> str:
    """Why a dead run may not be re-dispatched now — the claim composition, failing CLOSED when unreadable."""
    from teatree.core.managers import claim_admission_block_reason  # noqa: PLC0415 — deferred: needs the app registry

    try:
        return claim_admission_block_reason()
    except Exception as exc:
        logger.exception("reap_stuck_runs: admission read failed; dead runs are held")
        return f"{type(exc).__name__}: {exc}"


def reap_stuck_runs() -> dict[str, int]:
    """Fail dead-worker ``execute_task`` runs and re-enqueue their live tasks (#10).

    ``timer_reconciler`` recovers only stranded ``loop_timer`` rows, and
    ``expire_stale_ready_jobs`` touches only READY rows — so a ``DBTaskResult``
    left RUNNING when a worker died mid-run wedges forever: the Task's lease is
    reclaimed back to PENDING but ``execute_task``'s auto-enqueue fires
    only on post_save creation, so it is never re-run. This reaper closes that
    gap: each RUNNING ``execute_task`` past its lease+grace with a dead
    heartbeat is marked FAILED (reversible, inspectable — no hard delete), and
    when its ``Task`` row is still non-terminal a fresh ``execute_task``
    is enqueued so the work resumes. The claim CAS in ``execute_task``
    makes a redundant re-enqueue safe (a second run loses the claim and fails
    cleanly, never double-executes).

    The invariant this must not break: it may not create a second executor for work that
    is still executing (#4164). The claim CAS alone does not give that — it treats an
    EXPIRED lease as claimable, so against a live-but-stalled run the duplicate WINS the
    claim and the original aborts, briefly putting two agents on one worktree.
    :func:`_headless_run_is_dead` therefore probes the owner process too, and a
    stalled-but-alive run is left entirely alone — nothing failed, nothing enqueued — so
    the concurrent-worktree hazard cannot arise from this reaper.

    While no work is admitted (the ``off`` posture, a quiescing worker) a dead run is held
    untouched, so its replacement is enqueued exactly once when admission returns.
    """
    from django.tasks import TaskResultStatus  # noqa: PLC0415 — deferred: Django import at call time
    from django_tasks_db.models import DBTaskResult  # noqa: PLC0415 — deferred: Django import at call time

    from teatree.core.models import Task  # noqa: PLC0415 — deferred: ORM import needs the app registry
    from teatree.core.task_dispatch import enqueue_execution  # noqa: PLC0415 — deferred: task-body import
    from teatree.core.tasks import execute_task  # noqa: PLC0415 — deferred: task-body import

    now = timezone.now()
    running = DBTaskResult.objects.filter(
        task_path=execute_task.module_path,
        status=TaskResultStatus.RUNNING,
    )
    counts = {"failed": 0, "reenqueued": 0}
    held = 0
    held_reason = ""
    for row in running:
        task_id = _headless_task_id(row)
        if task_id is None:
            continue  # a malformed row with no identifiable Task — leave it untouched.
        task = Task.objects.filter(pk=task_id).first()
        if not _headless_run_is_dead(task, row, now):
            continue
        with transaction.atomic():
            # Only the reaper that still sees the row RUNNING fails and replaces it, so a rival scan cannot double it.
            still_running = DBTaskResult.objects.filter(id=row.id, status=TaskResultStatus.RUNNING).first()
            if still_running is None:
                continue
            # Admission is read under the same lock as the write, so a stop landing after the scan still holds the run.
            if refusal := _dispatch_refusal():
                held += 1
                held_reason = refusal
                continue
            still_running.set_failed(
                _StuckHeadlessRunError(f"execute_task {row.id} RUNNING past lease+grace; worker dead")
            )
            counts["failed"] += 1
            if task is not None and task.status not in Task.Status.terminal():
                enqueue_execution(task.pk, task.phase)
                counts["reenqueued"] += 1
    if held:
        logger.info("reap_stuck_runs: holding %d dead run(s) while no work is admitted: %s", held, held_reason)
    if any(counts.values()):
        logger.info("reap_stuck_runs: %s", counts)
    return counts


@task(queue_name=LOOPS_QUEUE)
def drain_chain() -> dict[str, int]:
    """Re-schedule ~5min out, THEN reap dead headless runs and drain the pending backlog.

    The scheduled home of ``drain_queue`` — it was defined but NEVER
    scheduled from anywhere, so the pending headless backlog only drained on the
    post_save auto-enqueue (missed on a lease-reclaim / stale interactive row).
    Seeded by :func:`ensure_maintenance_chains` at worker startup and
    self-perpetuating, like its sibling reconcile/prune/expire chains. Runs on
    the ``loops`` queue and enqueues onto ``default`` (it never runs the heavy
    headless work itself). Self-dedups first so an at-least-once redelivery
    collapses to one. Successor-FIRST (F6): the next fire is queued before the
    reap/drain body, in a try that records-but-never-propagates, so a body fault
    cannot orphan the chain.

    It carries BOTH reapers: :func:`reap_stuck_runs` for ``execute_task``, whose
    process-aware predicate can re-dispatch what it frees, and
    :func:`~teatree.loops.stranded_runs.reap_stranded_runs` for every other task path,
    which nothing else could reach at all.
    """
    from teatree.core.tasks import drain_queue_body  # noqa: PLC0415 — deferred: task-body import
    from teatree.loops.stranded_runs import reap_stranded_runs  # noqa: PLC0415 — deferred: cycle-safe

    if _pending_for_path(drain_chain.module_path):
        return {"deduped": 1}
    drain_chain.using(run_after=timezone.now() + dt.timedelta(seconds=DRAIN_INTERVAL_SECONDS)).enqueue()
    try:
        reaped = reap_stuck_runs()
        stranded = reap_stranded_runs()
        drained = drain_queue_body()
    except Exception:
        logger.exception("drain_chain body failed; successor already queued, the chain survives")
        return {"error": 1}
    return {
        "reaped_failed": reaped["failed"],
        "reaped_reenqueued": reaped["reenqueued"],
        "stranded_failed": stranded["failed"],
        "drained": len(drained["enqueued"]),
    }


def _run_slack_answer_cycle_under_lease() -> dict[str, int]:
    """Run one Slack-answer cycle under the shared ``loop-slack-answer`` lease.

    The single body both the cadence chain (:func:`run_slack_answer`) and the
    event-driven wake (:func:`wake_slack_answer`) share: each serialises on the
    SAME lease the mgmt command / interactive ``/loop`` slot takes, so the
    reactive worker, an owner session, and an inbound-event wake can never
    double-post. A held lease means a holder is already running the cycle, so
    this returns ``{"skipped_lease_held": 1}`` without running it; the caller
    decides whether to re-arm.
    """
    from teatree.core.models import LoopLease  # noqa: PLC0415 — deferred: ORM import needs the app registry
    from teatree.loop.slack_answer.cycle import run_slack_answer_cycle  # noqa: PLC0415 — deferred: task-body import

    owner = f"worker-{os.getpid()}"
    if not LoopLease.objects.acquire(SLACK_ANSWER_LEASE, owner=owner):
        return {"skipped_lease_held": 1}
    try:
        report = run_slack_answer_cycle()
        return {
            "processed": report.processed,
            "eyes_reacted": report.eyes_reacted,
            "acked": report.acked,
            "answered_simple": report.answered_simple,
            "dispatched": report.dispatched,
            "covered": report.covered,
            "answered_question": report.answered_question,
            "errors": report.errors,
        }
    finally:
        LoopLease.objects.release(SLACK_ANSWER_LEASE, owner=owner)


@task(queue_name=LOOPS_QUEUE)
def run_slack_answer() -> dict[str, int]:
    """Re-schedule at its cadence, THEN run one reactive Slack-answer cycle headless.

    Self-dedups first (another pending run carries the chain), mirroring the
    reconcile/prune/expire contract, so an at-least-once redelivery collapses to
    one. Successor-FIRST (F6): the next fire is queued before the cycle body, in a
    try that records-but-never-propagates, so a body fault cannot orphan the chain.
    The body runs :func:`_run_slack_answer_cycle_under_lease` — which SKIPS the
    cycle when an owner session already holds the ``loop-slack-answer`` lease rather
    than double-post. This cadence chain is the fallback safety net behind the
    event-driven :func:`wake_slack_answer`: it drains anything a missed wake left
    behind even when no inbound event arrives.
    """
    from teatree.loop.loop_cadences import slack_answer_cadence_seconds  # noqa: PLC0415 — deferred: tick-time import

    if _pending_for_path(run_slack_answer.module_path):
        return {"deduped": 1}

    run_slack_answer.using(
        run_after=timezone.now() + dt.timedelta(seconds=slack_answer_cadence_seconds()),
    ).enqueue()
    try:
        return _run_slack_answer_cycle_under_lease()
    except Exception:
        logger.exception("run_slack_answer body failed; successor already queued, the chain survives")
        return {"error": 1}


def _scan_label(value: object) -> str:
    return value if isinstance(value, str) and _SAFE_SCAN_LABEL.fullmatch(value) else "unknown"


def _run_self_improve_cycle_via_command() -> dict[str, int]:
    """Use the command's t3-master gate, lease, owner delivery, and scan report."""
    output = io.StringIO()
    call_command("loop_self_improve", tier=SELF_IMPROVE_TIER, json_output=True, stdout=output, stderr=io.StringIO())
    payload = json.loads(output.getvalue())
    if not isinstance(payload, dict):
        msg = "self-improve command did not return a JSON object"
        raise TypeError(msg)
    degraded = payload.get("degraded_scans")
    if isinstance(degraded, list) and degraded:
        summary = ",".join(
            f"{_scan_label(row.get('detector'))}:{_scan_label(row.get('reason'))}"
            for row in degraded[:5]
            if isinstance(row, dict)
        )
        logger.warning("unattended self-improve scan degraded (%d sources): %s", len(degraded), summary)
        return {"ran": 1, "degraded": len(degraded)}
    if payload.get("skipped") or payload.get("skipped_reason"):
        logger.warning("unattended self-improve cycle skipped")
        return {"skipped": 1}
    return {"ran": 1}


@task(queue_name=LOOPS_QUEUE)
def run_self_improve() -> dict[str, int]:
    """Re-schedule at its cadence, THEN run one cheap-tier self-improve cycle headless.

    The third reactive slot to move onto the worker. Slack-answer and drain-queue
    already ran as worker chains, so self-improve was the only one a session still had
    to register a ``/loop`` for — which is why the session-setup prose kept asking for
    all three and recurrently produced hand-run loop crons (#2663). Successor-FIRST and
    self-deduping, so a body fault never orphans the chain.
    """
    from teatree.loop.loop_cadences import self_improve_cadence_seconds  # noqa: PLC0415 — deferred: tick-time import

    if _pending_for_path(run_self_improve.module_path):
        return {"deduped": 1}

    run_self_improve.using(
        run_after=timezone.now() + dt.timedelta(seconds=self_improve_cadence_seconds()),
    ).enqueue()
    try:
        return _run_self_improve_cycle_via_command()
    except Exception:
        logger.exception("run_self_improve body failed; successor already queued, the chain survives")
        return {"error": 1}


@task(queue_name=LOOPS_QUEUE)
def wake_slack_answer() -> dict[str, int]:
    """Run ONE Slack-answer cycle immediately, triggered by an inbound Slack event.

    Enqueued (best-effort) by the Socket Mode receiver — via a callback wired at
    the CLI composition root, since the receiver's ``backends`` layer cannot
    reach this orchestration layer — the moment it appends an inbound event to
    the durable JSONL queue, so a reply lands in ~one worker poll interval
    instead of waiting out the :func:`run_slack_answer` cadence. Runs the same
    lease-guarded cycle as the cadence chain (so it can never double-post), then
    STOPS — it does not re-arm. The cadence chain remains the fallback that
    drains anything a missed wake left behind.

    Two dedupes, because a pending wake is only half the burst. Against a
    PENDING wake: the queued one carries the work, so this one stands down.
    Against a RECENTLY FINISHED one: a cycle takes ~2s, so events arriving
    slower than that found nothing pending and bought a cycle each — 290 an
    hour at the peak of #4707. Inside the interval the wake re-arms itself once
    at the far edge and returns, which is a trailing-edge debounce: the burst
    costs O(1) cycles and its last event is still answered, one interval late
    at worst rather than one cadence.
    """
    if _pending_for_path(wake_slack_answer.module_path):
        return {"deduped": 1}
    last_finished = _cycle_finished_within(wake_slack_answer.module_path, WAKE_MIN_INTERVAL_SECONDS)
    if last_finished is not None:
        wake_slack_answer.using(run_after=last_finished + dt.timedelta(seconds=WAKE_MIN_INTERVAL_SECONDS)).enqueue()
        return {"coalesced": 1}
    return _run_slack_answer_cycle_under_lease()


def ensure_maintenance_chains() -> None:
    """Seed every maintenance chain if absent.

    Reconcile, prune, expire, drain, slack-answer, self-improve, off-live-tick drive,
    usage-window recovery, preset transitions, and statusline refresh.
    """
    from teatree.loop.loop_cadences import (  # noqa: PLC0415 — deferred: tick-time import
        self_improve_cadence_seconds,
        slack_answer_cadence_seconds,
    )
    from teatree.loops.off_live_tick_driver import ensure_off_live_tick_driver_chain  # noqa: PLC0415 — cycle-safe
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
    # #10: the headless-queue drain + dead-run reaper. ``drain_queue``
    # had zero call sites, so the pending headless backlog only drained on the
    # post_save auto-enqueue — a lease-reclaimed or stale-interactive row was
    # never re-dispatched. Seeding it here is the "actually run the drain" fix.
    if not _pending_for_path(drain_chain.module_path):
        drain_chain.using(run_after=now + dt.timedelta(seconds=DRAIN_INTERVAL_SECONDS)).enqueue()
    # The reactive Slack-answer cycle, armed headless so the worker drains the
    # 👀-receipt + reply/delegate machinery that only ran in an interactive owner
    # session's ``/loop`` slot before. Lease-guarded against the owner session.
    if not _pending_for_path(run_slack_answer.module_path):
        run_slack_answer.using(run_after=now + dt.timedelta(seconds=slack_answer_cadence_seconds())).enqueue()
    # The self-improve cycle, armed headless for the same reason: it was the last
    # reactive slot still depending on a live session to register its ``/loop``.
    if not _pending_for_path(run_self_improve.module_path):
        run_self_improve.using(run_after=now + dt.timedelta(seconds=self_improve_cadence_seconds())).enqueue()
    # The off-live-tick driver. Without it directive_loop / dream / outer_loop have NO
    # driver at all: the live fan-out excludes them, the reconciler above builds them no
    # chain, and the cron their docstrings promised was never installed anywhere.
    ensure_off_live_tick_driver_chain()
    # Directive #3: the self-rescheduling usage-window re-arm chain. Its body is inert while
    # is unconditional now, so seeding it needs no flag read.
    ensure_usage_window_recovery_chain()
    # #3159: the preset-transition side-effect chain (override reap, availability pin,
    # one Slack line per switch). Inert with no active preset — a cheap keepalive.
    ensure_preset_transitions_chain()
    # The headless statusline-render chain — keeps ``statusline.txt`` fresh on a short
    # cadence even when NO domain loop is admitted-and-ticking (the true cause of the
    # long-standing stale-loop-line complaint), gated by the ``autoload`` #256 flag.
    ensure_statusline_refresh_chain()
