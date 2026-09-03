"""The backstop reaper for a RUNNING ``DBTaskResult`` no per-path reaper can reach.

Every existing reaper is keyed to one task path or one status, and
:data:`~teatree.core.tasks.STRANDED_JOB_GRACE_SECONDS` already names the consequence
in its own docstring — "every reaper is per-task-path — so anything treating RUNNING as
live needs a bound". :func:`~teatree.loops.timer_reconciler.ensure_loop_timers` repairs a
stranded ``loop_timer`` only for an ADMITTED loop, :func:`~teatree.loops.timer_reconciler.
reap_stuck_runs` filters on ``execute_task``, :func:`~teatree.loop.queue_drain.
expire_stale_ready_jobs` retires only READY rows, and
:func:`~teatree.core.retention.task_results.prune_finished_task_results` resolves through
the library's ``finished()`` predicate, which excludes READY and RUNNING alike. A row from
any other path is therefore unreachable by all of them and survives indefinitely — measured
on the running box: 33 such rows across seven task paths, the oldest 26 days.

Two of those seven paths (``drain_headless_chain``, ``execute_headless_task``) exist in no
module any more, and one row carries the ``pane_reaper`` loop that migration 0033 retired.
That is the argument against a hand-maintained path-to-deadline table: it would go stale
exactly as those names did, and the rows it stopped covering are the ones that leak. The
ceiling here is resolved from the QUEUE the row itself carries, so a renamed or deleted
path is still bounded.

**The queue is the bound because the queue is what caps the body.** A ``loops`` row drives
deadlined tick subprocesses, so its ceiling is the budget those subprocesses can legitimately
consume — computed from the registry (:func:`~teatree.loops.off_live_tick_driver.
off_live_tick_commands`), never configured, so a fourth off-live-tick loop raises it on its
own. Everything else is an FSM job on ``default``, whose bound is
:data:`~teatree.core.tasks.STRANDED_JOB_GRACE_SECONDS` — the value
:meth:`~teatree.core.tasks.TeardownDispatch.outstanding_for` and the doctor's stranded
probe ALREADY judge a RUNNING row against. Reusing it means this reaper writes back the
verdict two readers hold rather than inventing a second policy.

**Age is necessary, never sufficient.** Every RUNNING row names its own carrier, because
``DBTaskResult.claim`` is the only path to RUNNING and it stamps the claiming process's
worker id: ``worker-<pid>-<index>-<queue>`` from the singleton worker's executors,
``tickdrain-<pid>-<id>`` from the tick drain. A row is retired only once that carrier is
PROVED gone — the ceiling is a heuristic over a body's plausible runtime, the process
table is a fact. A carrier this cannot judge keeps its row and is named in the log.

**What it deliberately does not do.** It never touches ``execute_task``:
:func:`~teatree.loops.timer_reconciler.reap_stuck_runs` owns that path with a process-aware
predicate because a stalled-but-ALIVE headless run must be left entirely alone — a flat age
ceiling cannot tell it from a corpse, and failing it hands the claim CAS to a duplicate that
lands a second agent on one worktree (#4164). And it never re-enqueues: marking FAILED is
bookkeeping that kills nothing, while minting a successor would create exactly the second
executor this reaper must not risk. Re-dispatch stays with the reapers that can prove
liveness.
"""

import datetime as dt
import logging

from django.db import transaction
from django.db.utils import OperationalError
from django.utils import timezone

from teatree.core.loop_lease_liveness import owner_pid_is_dead

logger = logging.getLogger(__name__)

#: The worker-id kinds that stamp their claiming pid into the id, and the one of them
#: the flock singleton bounds to a single live process at a time.
_SINGLETON_WORKER_KIND = "worker"
_CLAIMANT_KINDS = (_SINGLETON_WORKER_KIND, "tickdrain")


class StrandedRunError(RuntimeError):
    """Recorded on a RUNNING result reaped for outliving its queue's healthy ceiling."""


def stranded_after_seconds(queue_name: str) -> float:
    """How long a RUNNING row on *queue_name* may plausibly still be in flight."""
    from teatree.core.tasks import STRANDED_JOB_GRACE_SECONDS  # noqa: PLC0415 — deferred: needs the app registry
    from teatree.loops.off_live_tick_driver import (  # noqa: PLC0415 — deferred: the walk imports every loop module
        DEADLINE_SECONDS,
        off_live_tick_commands,
    )
    from teatree.loops.timer_chains import LOOPS_QUEUE  # noqa: PLC0415 — deferred: loaded at tick time
    from teatree.loops.timer_reconciler import STUCK_GRACE_SECONDS  # noqa: PLC0415 — deferred: cycle-safe

    if queue_name != LOOPS_QUEUE:
        return float(STRANDED_JOB_GRACE_SECONDS)

    # Floored on the grace so a registry that yields nothing cannot collapse the ceiling
    # to zero and reap every live chain row on the next tick.
    budget = max(float(STRANDED_JOB_GRACE_SECONDS), len(off_live_tick_commands()) * float(DEADLINE_SECONDS))
    return budget + STUCK_GRACE_SECONDS


def _worker_singleton_pid() -> int | None:
    """The pid of the LIVE worker holding the singleton, or ``None`` when unreadable."""
    from teatree.utils.singleton import (  # noqa: PLC0415 — deferred: call-time import, kept lazy
        WORKER_SINGLETON,
        default_pid_path,
        read_pid,
    )

    return read_pid(default_pid_path(WORKER_SINGLETON))


def _claiming_pid(worker_id: str) -> int | None:
    """The pid a ``DBTaskResult`` worker id carries, or ``None`` when it carries none."""
    kind, _, tail = worker_id.partition("-")
    if kind not in _CLAIMANT_KINDS:
        return None
    pid = tail.partition("-")[0]
    return int(pid) if pid.isdigit() else None


def _carrier_is_gone(worker_id: str) -> bool:
    """Whether the process that claimed a row is PROVED to have exited.

    A ``worker-`` claim is decided against the singleton's own pid file rather than the
    pid probe: at most one worker holds the flock, so a claim by any pid other than the
    live holder's was made by a replaced worker and is gone even where the OS has since
    reused that integer — which the probe alone cannot tell from a live carrier. Every
    other claim falls back to the probe, whose null and unprobeable answers are
    indeterminate and therefore never proof.
    """
    pid = _claiming_pid(worker_id)
    if pid is None:
        return False
    if worker_id.startswith(f"{_SINGLETON_WORKER_KIND}-"):
        holder = _worker_singleton_pid()
        if holder is not None:
            return pid != holder
    return owner_pid_is_dead(pid)


def _fail_if_still_running(row_id: str, reason: StrandedRunError) -> bool:
    """Mark one row FAILED only while it is STILL RUNNING; return whether it did.

    ``set_failed`` is an unconditional save, so retiring the instance the scan read would
    clobber a row that finished in between. Re-reading inside the write transaction turns
    that into a compare-and-swap, mirroring :func:`~teatree.loop.queue_drain._fail_if_still_ready`.
    """
    from django_tasks.base import TaskResultStatus  # noqa: PLC0415 — deferred: heavy/optional dep at call site
    from django_tasks_db.models import DBTaskResult  # noqa: PLC0415 — deferred: heavy/optional dep at call site

    try:
        with transaction.atomic():
            still_running = DBTaskResult.objects.filter(id=row_id, status=TaskResultStatus.RUNNING).first()
            if still_running is None:
                return False
            still_running.set_failed(reason)
    except OperationalError as exc:
        logger.warning("Could not reap stranded run %s: %s", row_id, exc)
        return False
    return True


def reap_stranded_runs(now: dt.datetime | None = None) -> dict[str, int]:
    """Fail every RUNNING row past its queue's ceiling whose carrier is gone; return how many went.

    FAILED is reversible and auditable — the row, its args and the reason all survive — so
    a ceiling that proves too tight is recoverable by re-enqueue rather than by archaeology.
    """
    from django_tasks.base import TaskResultStatus  # noqa: PLC0415 — deferred: heavy/optional dep at call site
    from django_tasks_db.models import DBTaskResult  # noqa: PLC0415 — deferred: heavy/optional dep at call site

    from teatree.core.tasks import execute_task  # noqa: PLC0415 — deferred: task-body import

    moment = now or timezone.now()
    counts = {"failed": 0}
    unjudged: list[str] = []
    running = DBTaskResult.objects.filter(status=TaskResultStatus.RUNNING).exclude(task_path=execute_task.module_path)
    for row in running:
        # A row that never recorded a start cannot be aged; the reaper that owns its path
        # is the one entitled to judge it on any other evidence.
        if row.started_at is None:
            continue
        ceiling = stranded_after_seconds(row.queue_name)
        if row.started_at > moment - dt.timedelta(seconds=ceiling):
            continue
        carrier = row.worker_ids[-1] if row.worker_ids else ""
        if not _carrier_is_gone(carrier):
            unjudged.append(f"{row.id} ({row.task_name}, carrier {carrier or 'unrecorded'})")
            continue
        reason = StrandedRunError(
            f"{row.task_name} ({row.task_path}) RUNNING since {row.started_at.isoformat()} on queue "
            f"{row.queue_name!r}, past the {ceiling:.0f}s ceiling; carrier {carrier} is gone."
        )
        counts["failed"] += int(_fail_if_still_running(row.id, reason))
    if unjudged:
        logger.info(
            "reap_stranded_runs: kept %d row(s) past their ceiling whose carrier is not proved gone: %s",
            len(unjudged),
            "; ".join(unjudged),
        )
    if counts["failed"]:
        logger.info("reap_stranded_runs: %s", counts)
    return counts


__all__ = ["StrandedRunError", "reap_stranded_runs", "stranded_after_seconds"]
