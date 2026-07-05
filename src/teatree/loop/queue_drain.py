"""Tick-driven draining and stale-job expiry for the django-tasks DB queue.

The DB-backed django-tasks queue (``DBTaskResult``, backend
``django_tasks_db.DatabaseBackend``) only advances when *something* drains
it. ``t3 <overlay> worker`` spawns ``manage.py db_worker`` subprocesses, but
that is a manual machine-wide singleton nobody keeps alive — so enqueued
``execute_headless_task`` / ``execute_provision`` / ``execute_ship`` jobs sit
in ``READY`` forever (the #786 loop is tick-driven and session-bound; it
assumes no always-on OS daemon).

This module closes that gap inside the existing tick, with no new daemon:

:func:`drain_ready_batch` runs a *bounded* batch of READY jobs in-process,
mirroring ``db_worker``'s claim/run/finish semantics (lock the row, claim it so
a concurrent drainer can't double-run it, execute, mark successful/failed). It
idles cleanly — an empty queue returns immediately.

:func:`expire_stale_ready_jobs` retires READY jobs older than a threshold by
marking them ``FAILED`` (the queue's only terminal non-run state) with a
descriptive traceback, so a freshly-supervised drainer never blind-fires heavy
provision/ship/teardown jobs that have been queued for days. FAILED is
reversible — the row, its args, and the reason are all preserved; nothing is
hard-deleted.

Both are driven by the dedicated reactive drain-queue ``/loop``
(``t3 loop drain-queue run`` → the ``loop_drain_queue`` management command →
:func:`expire_then_drain`, behind the ``loop-drain-queue`` ``LoopLease``). The drain
refuses to run while a live worker holds either worker-singleton flock
(:data:`~teatree.utils.singleton.WORKER_SINGLETON` or the legacy
:data:`~teatree.utils.singleton.LEGACY_WORKER_SINGLETON` — probed via the same
constants the workers acquire), and it only drains the ``default`` queue — the
``loops``-queue ``loop_timer`` rows advance ONLY on the worker's pinned executors, so
the drain cannot become a second loop runner that bypasses the ``loop_runner_enabled``
kill-switch.
"""

import datetime as dt
import logging
import os
from typing import TYPE_CHECKING

from django.core.exceptions import SuspiciousOperation
from django.db.models import Case, IntegerField, Q, Value, When
from django.db.utils import OperationalError
from django.utils import timezone

if TYPE_CHECKING:
    from teatree.core.managers import ClaimOrder

logger = logging.getLogger(__name__)

_STALE_THRESHOLD_DEFAULT_HOURS = 24
_DRAIN_BATCH_DEFAULT = 5

#: Priority order the loop admits pending Task rows in: TODO/followup (rank 0)
#: before a new-ticket auto-start (rank 1), then FIFO ``pk`` within a rank.
ADMISSION_RANK_ALIAS = "_admission_rank"
ADMISSION_ORDER: tuple[str, ...] = (ADMISSION_RANK_ALIAS, "pk")


def _new_ticket_autostart_q() -> Q:
    """A task that auto-STARTS a brand-new ticket: an initial-phase, un-parented row.

    A ``planning``/``scoping`` task with no ``parent_task`` is the first phase of
    a freshly picked-up ticket. Everything else — a downstream lifecycle phase
    (coding/testing/reviewing/shipping), a followup (``parent_task`` set), or a
    reactive ``answering``/``bughunt`` task — is continuing TODO work that should
    drain first. Matched across every accepted spelling so a short-verb
    ``plan``/``scope`` row ranks identically to the canonical gerund.
    """
    from teatree.core.modelkit.phases import phase_spellings  # noqa: PLC0415

    autostart_phases = phase_spellings("planning") + phase_spellings("scoping")
    return Q(parent_task__isnull=True) & Q(phase__in=autostart_phases)


def admission_priority_annotations() -> dict[str, Case]:
    """The ``.annotate()`` kwargs producing the integer :data:`ADMISSION_RANK_ALIAS`.

    ``0`` = TODO/followup (drain first); ``1`` = new-ticket auto-start. Paired
    with :data:`ADMISSION_ORDER` on the claim/plan path so a queued TODO admits
    before a lower-``pk`` new-ticket task at equal priority.
    """
    return {
        ADMISSION_RANK_ALIAS: Case(
            When(_new_ticket_autostart_q(), then=Value(1)),
            default=Value(0),
            output_field=IntegerField(),
        )
    }


def admission_claim_order() -> "ClaimOrder":
    """The :class:`ClaimOrder` the loop passes to ``claim_next_pending`` (PR-13).

    Bundles :func:`admission_priority_annotations` with :data:`ADMISSION_ORDER`
    so the live claim path admits a queued TODO/followup before a new-ticket
    auto-start.
    """
    from teatree.core.managers import ClaimOrder  # noqa: PLC0415

    return ClaimOrder(annotations=admission_priority_annotations(), order_by=ADMISSION_ORDER)


class StaleQueueJobError(RuntimeError):
    """Recorded as the failure reason on a READY job retired for being stale.

    Carries a terminal, non-run outcome onto the ``DBTaskResult`` row via
    ``set_failed`` — the row, its args, and this reason survive, so the
    expiry is auditable and reversible (re-enqueue is possible) rather than a
    hard delete.
    """


def stale_threshold_hours() -> int:
    """Stale-READY age threshold in hours (``T3_QUEUE_STALE_HOURS``, default 24, floor 1).

    A blank or non-integer override degrades to the default rather than
    crashing the tick; the 1h floor keeps a fat-fingered ``0`` from retiring
    jobs the instant they are enqueued.
    """
    raw = os.environ.get("T3_QUEUE_STALE_HOURS", str(_STALE_THRESHOLD_DEFAULT_HOURS)).strip()
    if not raw:
        return _STALE_THRESHOLD_DEFAULT_HOURS
    try:
        return max(1, int(raw))
    except ValueError:
        return _STALE_THRESHOLD_DEFAULT_HOURS


def drain_batch_size() -> int:
    """Per-tick in-process drain batch size (``T3_QUEUE_DRAIN_BATCH``, default 5, floor 1).

    Bounded so a single tick never blocks for the whole backlog: each won
    tick drains at most this many jobs, and the next tick picks up where it
    left off.
    """
    raw = os.environ.get("T3_QUEUE_DRAIN_BATCH", str(_DRAIN_BATCH_DEFAULT)).strip()
    if not raw:
        return _DRAIN_BATCH_DEFAULT
    try:
        return max(1, int(raw))
    except ValueError:
        return _DRAIN_BATCH_DEFAULT


def a_worker_is_running() -> bool:
    """True iff a live worker holds either worker-singleton flock.

    Two worker entry points can own the queue: the #1796 ``LoopWorker``
    acquires :data:`~teatree.utils.singleton.WORKER_SINGLETON` (``"worker"``),
    while the older ``t3 <overlay> worker`` spawner still acquires
    :data:`~teatree.utils.singleton.LEGACY_WORKER_SINGLETON` (``"teatree-worker"``)
    during the deprecation window. The probe imports the SAME constants the
    workers acquire — so the name can never drift — and stands the in-process
    tick drain down when either is alive, so the two never claim the same rows.
    ``read_pid`` reports the live holder (and reaps a stale pid file) without
    acquiring the lock, so probing here never disturbs a running worker.
    """
    from teatree.utils.singleton import (  # noqa: PLC0415 — deferred: keeps queue_drain cold-import cheap
        LEGACY_WORKER_SINGLETON,
        WORKER_SINGLETON,
        default_pid_path,
        read_pid,
    )

    return any(read_pid(default_pid_path(name)) is not None for name in (WORKER_SINGLETON, LEGACY_WORKER_SINGLETON))


def expire_stale_ready_jobs(*, threshold_hours: int | None = None, queue_name: str | None = None) -> dict[str, int]:
    """Retire READY jobs older than the threshold, returning a count by task name.

    Conservative by design: only ``READY`` jobs whose ``enqueued_at`` predates
    ``now - threshold`` are touched; RUNNING/finished jobs and fresh READY jobs
    are left alone. Each retired job is marked ``FAILED`` via ``set_failed`` —
    the queue's terminal non-run state — carrying a :class:`StaleQueueJobError`
    so the reason is recorded and the row stays inspectable/re-enqueueable. No
    hard delete.

    ``queue_name`` scopes the sweep to one queue when given. The worker's
    startup + hourly expiry pass the ``default`` queue (via
    :func:`expire_stale_default_jobs`) so it never touches the ``loops``-queue
    timer chains — those are owned by the reconciler's own staleness repair
    (stranded-RUNNING / surplus prune), and a shared cutoff sweep would fight it.
    """
    from django_tasks.base import TaskResultStatus  # noqa: PLC0415
    from django_tasks_db.models import DBTaskResult  # noqa: PLC0415

    hours = threshold_hours if threshold_hours is not None else stale_threshold_hours()
    cutoff = timezone.now() - dt.timedelta(hours=hours)
    stale = DBTaskResult.objects.filter(status=TaskResultStatus.READY, enqueued_at__lt=cutoff)
    if queue_name is not None:
        stale = stale.filter(queue_name=queue_name)

    retired: dict[str, int] = {}
    for job in stale.iterator():
        name = job.task_name
        reason = StaleQueueJobError(
            f"Expired READY job {job.id} ({name}): enqueued {job.enqueued_at.isoformat()}, "
            f"older than the {hours}h stale threshold; retired without running."
        )
        try:
            job.set_failed(reason)
        except OperationalError as exc:
            logger.warning("Could not expire stale job %s (%s): %s", job.id, name, exc)
            continue
        retired[name] = retired.get(name, 0) + 1
    if retired:
        logger.info("Expired %d stale READY job(s) older than %dh: %s", sum(retired.values()), hours, retired)
    return retired


def expire_stale_default_jobs() -> dict[str, int]:
    """Retire stale READY jobs on the ``default`` queue only — the heavy FSM/headless backlog.

    The worker's startup expiry (before it spawns executors) and its hourly
    maintenance chain both call this so a box that accumulated days-old
    provision/ship/teardown jobs while no worker ran does NOT blind-fire them the
    instant the worker spawns (the default-ON flip's load-jam class). Scoped to the
    ``default`` queue so the reconciler stays the sole owner of ``loops``-queue timer
    staleness.
    """
    from django_tasks import DEFAULT_TASK_QUEUE_NAME  # noqa: PLC0415 — deferred: needs the app registry ready

    return expire_stale_ready_jobs(queue_name=DEFAULT_TASK_QUEUE_NAME)


def _run_one_ready_job() -> bool:
    """Claim and run a single READY job, mirroring ``db_worker.run_task``.

    Returns ``True`` if a job was claimed and executed (success OR failure —
    both are terminal outcomes that drain the queue), ``False`` when no READY
    job was available. The row is locked + claimed inside an exclusive
    transaction so a concurrent drainer cannot pick the same job.

    Only the ``default`` queue is drained: the self-rescheduling ``loop_timer``
    chains ride the separate ``loops`` queue and run ONLY on the worker's pinned
    executors, so the tick drain never becomes an accidental loop runner that
    bypasses the ``loop_runner_enabled`` kill-switch. ``"loops"`` is the only
    non-``default`` queue, so scoping the claim to ``DEFAULT_TASK_QUEUE_NAME``
    leaves the timer rows for the worker.
    """
    from django.db import close_old_connections  # noqa: PLC0415
    from django_tasks import (  # noqa: PLC0415 — deferred: django_tasks needs the app registry ready
        DEFAULT_TASK_BACKEND_ALIAS,
        DEFAULT_TASK_QUEUE_NAME,
    )
    from django_tasks.signals import task_finished, task_started  # noqa: PLC0415
    from django_tasks.utils import get_random_id  # noqa: PLC0415
    from django_tasks_db.models import DBTaskResult  # noqa: PLC0415
    from django_tasks_db.utils import exclusive_transaction  # noqa: PLC0415

    worker_id = f"tickdrain-{os.getpid()}-{get_random_id()}"
    ready = (
        DBTaskResult.objects.ready()
        .filter(backend_name=DEFAULT_TASK_BACKEND_ALIAS)
        .filter(queue_name=DEFAULT_TASK_QUEUE_NAME)
    )

    with exclusive_transaction(ready.db):
        try:
            job = ready.get_locked()
        except OperationalError as exc:
            if "is locked" in exc.args[0]:
                return False
            raise
        if job is None:
            return False
        job.claim(worker_id)

    try:
        task = job.task
        task_result = job.task_result
        backend_type = task.get_backend()
        task_started.send(sender=backend_type, task_result=task_result)
        if task.takes_context:
            from django_tasks.base import TaskContext  # noqa: PLC0415

            return_value = task.call(TaskContext(task_result=task_result), *task_result.args, **task_result.kwargs)
        else:
            return_value = task.call(*task_result.args, **task_result.kwargs)
        job.set_successful(return_value)
        task_finished.send(sender=backend_type, task_result=job.task_result)
    except BaseException as exc:  # noqa: BLE001 — match db_worker: any task error becomes a FAILED row, never crashes the drainer
        job.set_failed(exc)
        try:
            sender = type(job.task.get_backend())
            task_finished.send(sender=sender, task_result=job.task_result)
        except (ImportError, SuspiciousOperation):
            logger.exception("Drained task id=%s failed unexpectedly", job.id)
    finally:
        close_old_connections()
    return True


def drain_ready_batch(*, max_jobs: int | None = None) -> int:
    """Drain at most ``max_jobs`` READY jobs in-process; return how many ran.

    Stands down entirely when a real worker is alive (it owns the drain — see
    :func:`a_worker_is_running`). Stops early the moment the queue is empty, so
    an idle tick costs one ``ready()`` query and returns ``0``.
    """
    if a_worker_is_running():
        logger.debug("Skipping in-process queue drain: a live worker holds a worker singleton.")
        return 0
    limit = max_jobs if max_jobs is not None else drain_batch_size()
    drained = 0
    for _ in range(limit):
        if not _run_one_ready_job():
            break
        drained += 1
    if drained:
        logger.info("Tick drained %d queued job(s) in-process.", drained)
    return drained


def expire_then_drain() -> dict[str, int | dict[str, int]]:
    """Expire stale READY jobs, then drain a bounded batch of the fresh remainder.

    The expiry runs *first* so a stale heavy job (a 12-day-old provision/ship/
    teardown) is retired to ``FAILED`` before the drain can ever claim and run
    it. Only jobs newer than the stale threshold survive to be drained.
    """
    retired = expire_stale_ready_jobs()
    drained = drain_ready_batch()
    return {"retired": retired, "drained": drained}
