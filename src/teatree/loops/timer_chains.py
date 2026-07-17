"""Self-rescheduling loop-timer chains — durable, crash-surviving loop cadence (#1796).

Replaces the in-memory beat with django-tasks ``run_after`` rows: exactly one
pending ``loop_timer(name)`` task per enabled :class:`Loop` row on the dedicated
``loops`` queue is a durable timer that survives a crash — the DB row IS the
scheduled fire. When a worker executor drains it (its ``run_after`` has elapsed)
the task runs a five-step body that re-schedules its own successor BEFORE doing
the tick work, so a crash mid-tick always leaves a queued successor and the chain
never stalls.

The tick body is gated by the ``loop_runner_enabled`` kill-switch (step 0): a fire while
the switch is OFF returns immediately without re-enqueueing a successor, so flipping the
switch off terminates the chain at its source (not only at the worker supervisor). When
the switch is ON the five fixed steps run:

Step 1 — self-dedup: a second pending ``loop_timer`` for the same loop already
carries the chain, OR a concurrently-RUNNING duplicate with a lower id outranks this
fire, so this one stops without chaining (collapses duplicates to one — the "exactly
one live timer per loop" invariant self-heals; the id tiebreak lets exactly one of two
racing RUNNING timers proceed).

Step 2 — successor-first re-enqueue: schedule the next timer BEFORE running the
tick, so a crash during the tick leaves a queued successor (crash-safe). The
``run_after`` is floored at ``now + IDLE_POLL_FLOOR_SECONDS``: an already-due
successor scheduled at ``now`` is immediately READY, so a second ``loops`` executor
claims it and spawns a duplicate tick subprocess while this one is still in flight —
the floor holds the successor back until this tick has moved the anchor. A future
interval/daily slot beyond the floor still fires at that slot; step 5 refines the
successor to the precise next slot once the tick's CAS moves the anchor.

Step 3 — admission check: the unified enabled+due+reachable verdict
(:func:`teatree.loops.loop_table.admitted_loop_names`). A held/disabled/not-due loop
is a free no-op; its successor is refined to a polling floor so it never busy-spins.

Step 4 — deadlined subprocess tick: the tick runs as its OWN process group
subprocess (``python -m teatree loops_tick --loop <name>``) with a hard deadline —
``max(300 s, 3 x cadence)`` for an interval loop, the dedicated
:data:`DAILY_TICK_DEADLINE_SECONDS` for a ``daily_at`` loop; on expiry the whole group
is killed, so a hung tick occupies one executor slot for at most the deadline and every
other loop keeps firing. A tick killed at its deadline already consumed its cadence
anchor, so its work is lost until the next slot — that is escalated LOUDLY via a durable
``DeferredQuestion``, never left behind a silent warning.

Step 5 — post-tick refinement: after the tick's CAS bumps ``Loop.last_run_at``, the
successor's ``run_after`` is recomputed from the fresh anchor and pushed out to the
precise next slot. When the anchor did NOT move (a faulted tick — a crash before the
CAS, a connector outage, a lost lease), a still-"due" loop would recompute to ``now``
and re-spawn a full Django subprocess every few seconds, unbounded, for the fault's
duration; instead the successor is floored to the idle poll, so a fault costs one
poll per floor interval, never a subprocess hot-refire storm.

Idempotency is inherited: at-least-once delivery from django-tasks means a
``loop_timer`` can fire twice; the per-loop tick's ``mark_run_if_unchanged`` CAS
makes the redelivered run a no-op, and step 1's self-dedup collapses redundant
successors, so a double delivery never doubles the chain.
"""

import datetime as dt
import logging
import os
import signal
import sys
import threading
import uuid
from typing import TYPE_CHECKING, TypedDict

from django.tasks import task
from django.utils import timezone

from teatree.utils.run import Popen, TimeoutExpired, spawn_session_leader

if TYPE_CHECKING:
    from django_tasks_db.models import DBTaskResult

    from teatree.core.models import Loop

logger = logging.getLogger(__name__)


class TickOutcome(TypedDict):
    """The result of one deadlined subprocess tick."""

    timed_out: bool
    returncode: int | None


class TimerResult(TypedDict, total=False):
    """One ``loop_timer`` fire's outcome — the branch taken plus any tick result."""

    loop: str
    action: str
    timed_out: bool
    returncode: int | None


#: The dedicated django-tasks queue every loop timer chain rides. The worker pins
#: half its executor threads here so a reactive timer never blocks behind a heavy
#: ``default``-queue FSM/headless job. Mirrors the ``TASKS["default"]["QUEUES"]``
#: allowlist in ``teatree.settings`` (parity-tested).
LOOPS_QUEUE = "loops"

#: Set in the deadlined tick subprocess's environment so the ``loops_tick`` command can
#: ``os._exit`` right after rendering — a hung NON-daemon scanner thread would otherwise
#: block interpreter shutdown (its ``ThreadPoolExecutor`` atexit join), pinning the
#: subprocess (and one scarce ``loops`` executor slot) until the outer deadline SIGKILL.
#: Only the spawned subprocess carries it — an in-process ``call_command`` never does, so
#: tests never trip the hard exit.
TICK_SUBPROCESS_ENV_MARKER = "T3_LOOPS_TICK_SUBPROCESS"

#: A cadence-less (every-tick) loop has no interval, so its successor polls on this
#: floor rather than busy-spinning.
CADENCE_LESS_POLL_FLOOR_SECONDS = 60

#: A held/disabled/not-yet-due loop's successor is floored here so an idle chain
#: polls at a sane cadence instead of re-firing immediately.
IDLE_POLL_FLOOR_SECONDS = 60

#: The interval tick subprocess deadline is ``max(MIN_TICK_DEADLINE_SECONDS, 3 x cadence)``.
MIN_TICK_DEADLINE_SECONDS = 300.0
DEADLINE_CADENCE_MULTIPLIER = 3
#: A ``daily_at`` loop has no ``delay_seconds``, so ``3 x cadence`` collapses to the
#: 300 s floor — far too short for a daily news scan / sweep, which is then SIGKILLed
#: AFTER its cadence anchor was already consumed (loss for a full 24 h). Daily ticks
#: get their own generous deadline instead; a genuine overrun past it escalates loudly.
DAILY_TICK_DEADLINE_SECONDS = 1800.0


def _loop_runner_enabled() -> bool:
    """Whether the ``loop_runner_enabled`` kill-switch resolves ON (fail-safe OFF).

    The single reader the worker's executor pool AND every :func:`loop_timer` fire
    consult, so the kill-switch can never be honoured by one and silently bypassed by
    the other. A read failure degrades to OFF: a kill-switch that cannot confirm it is
    ON must not keep the chain alive.
    """
    try:
        from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred read

        return get_effective_settings().loop_runner_enabled
    except Exception:
        logger.debug("loop_runner_enabled read failed — treating the loop runner as disabled", exc_info=True)
        return False


def _loop_timer_path() -> str:
    """The dotted ``task_path`` django-tasks stores for :func:`loop_timer` rows."""
    return loop_timer.module_path


def _timers_for(name: str, *, status: str) -> "list[DBTaskResult]":
    """The ``loop_timer`` DBTaskResult rows for *name* in *status*.

    The DB pre-filters on ``task_path`` + ``status`` (small — at most a few timer
    rows per loop) and the loop-name match is done in Python against the stored
    ``args`` list, so the query stays backend-agnostic (no JSONField array-index
    lookup) and still exact.
    """
    from django_tasks_db.models import DBTaskResult  # noqa: PLC0415 — deferred: heavy/optional dep at call site

    rows = DBTaskResult.objects.filter(task_path=_loop_timer_path(), status=status)
    return [row for row in rows if row.args_kwargs.get("args") == [name]]


def pending_loop_timers(name: str) -> "list[DBTaskResult]":
    """READY (queued, not yet claimed) ``loop_timer`` rows for *name*."""
    from django_tasks.base import TaskResultStatus  # noqa: PLC0415 — deferred: heavy/optional dep at call site

    return _timers_for(name, status=TaskResultStatus.READY)


def running_loop_timers(name: str) -> "list[DBTaskResult]":
    """RUNNING (claimed, executing) ``loop_timer`` rows for *name*."""
    from django_tasks.base import TaskResultStatus  # noqa: PLC0415 — deferred: heavy/optional dep at call site

    return _timers_for(name, status=TaskResultStatus.RUNNING)


def _live_loop_timers(name: str) -> "list[DBTaskResult]":
    """READY-or-RUNNING ``loop_timer`` rows for *name* in ONE query.

    Step 1's self-dedup needs both the queued successor (READY) and any concurrent
    duplicate (RUNNING); fetching them together keeps the hot path at a single DB
    round-trip instead of two.
    """
    from django_tasks.base import TaskResultStatus  # noqa: PLC0415 — deferred: heavy/optional dep at call site
    from django_tasks_db.models import DBTaskResult  # noqa: PLC0415 — deferred: heavy/optional dep at call site

    rows = DBTaskResult.objects.filter(
        task_path=_loop_timer_path(), status__in=[TaskResultStatus.READY, TaskResultStatus.RUNNING]
    )
    return [row for row in rows if row.args_kwargs.get("args") == [name]]


def enqueue_loop_timer(name: str, *, run_after: dt.datetime) -> None:
    """Queue one ``loop_timer(name)`` timer on the ``loops`` queue at *run_after*."""
    loop_timer.using(run_after=run_after).enqueue(name)


def refine_successor(name: str, *, run_after: dt.datetime) -> None:
    """Push the pending successor timer(s) for *name* out to *run_after*.

    A direct ``run_after`` update on the READY rows — the post-tick cadence
    refinement (and the idle-poll floor for a skipped loop). A no-op when no
    successor is pending (the successor-first enqueue guarantees one under normal
    flow).
    """
    from django_tasks.base import TaskResultStatus  # noqa: PLC0415 — deferred: heavy/optional dep at call site
    from django_tasks_db.models import DBTaskResult  # noqa: PLC0415 — deferred: heavy/optional dep at call site

    ids = [row.id for row in _timers_for(name, status=TaskResultStatus.READY)]
    if ids:
        DBTaskResult.objects.filter(id__in=ids).update(run_after=run_after)


def compute_successor_run_after(row: "Loop", now: dt.datetime) -> dt.datetime:
    """When *row*'s next timer should fire — the conservative, crash-safe cadence.

    A future interval anchor or daily slot fires at that slot; a due/overdue
    interval loop and a never-run interval chain head both fire now; a cadence-less
    (no interval, no daily) loop polls on the 60 s floor so it never busy-spins.
    """
    nxt = row.next_run_at()
    if nxt is not None and nxt > now:
        return nxt
    if row.delay_seconds is None and row.daily_at is None:
        return now + dt.timedelta(seconds=CADENCE_LESS_POLL_FLOOR_SECONDS)
    return now


def _idle_successor_run_after(row: "Loop", now: dt.datetime) -> dt.datetime:
    """The successor cadence floored at ``now + IDLE_POLL_FLOOR_SECONDS``.

    A not-yet-due loop keeps its future slot; any already-due successor (a skipped
    held/disabled loop, the crash-safety successor of step 2, or a faulted tick whose
    anchor did NOT move in step 5) is floored so the chain polls rather than
    re-firing immediately — the single guard against an unbounded subprocess
    hot-refire when a loop stays "due".
    """
    return max(compute_successor_run_after(row, now), now + dt.timedelta(seconds=IDLE_POLL_FLOOR_SECONDS))


def compute_tick_deadline(row: "Loop") -> float:
    """The hard subprocess-tick deadline.

    An interval loop gets ``max(300 s, 3 x cadence)``. A ``daily_at`` loop has no
    ``delay_seconds`` (so ``3 x cadence`` would collapse to the 300 s floor and
    SIGKILL a legitimately long daily scan after its anchor was already consumed) —
    it gets the dedicated :data:`DAILY_TICK_DEADLINE_SECONDS` instead.
    """
    if row.delay_seconds is None and row.daily_at is not None:
        return DAILY_TICK_DEADLINE_SECONDS
    cadence = row.delay_seconds or 0
    return max(MIN_TICK_DEADLINE_SECONDS, DEADLINE_CADENCE_MULTIPLIER * float(cadence))


def _escalate_tick_timeout(name: str, *, deadline: float) -> None:
    """Record a durable escalation when a tick was SIGKILLed at its deadline, once per loop.

    A killed tick already consumed its cadence anchor (claimed BEFORE the scan in
    ``build_loop_table_jobs``), so this run's work is lost until the next slot — for a
    daily loop, a full 24 h, repeatable forever. That is exactly the "never silently
    freeze" invariant: the timeout must surface loudly, not sit behind a lone
    ``logger.warning``. Idempotent — a per-loop marker in the question text dedups
    across ALL questions (answered or not) so a repeatedly-timing-out loop escalates
    once, not every fire.
    """
    from teatree.core.models.deferred_question import DeferredQuestion  # noqa: PLC0415 — deferred: ORM import

    marker = f"[loop-tick-timeout loop={name}]"
    if DeferredQuestion.objects.filter(question__contains=marker).exists():
        return
    question = (
        f"{marker} Loop {name!r} tick exceeded its {deadline:.0f}s deadline and was killed; its cadence "
        "anchor was already consumed, so this run's work is lost until the next slot. Raise the loop's "
        "deadline or investigate why the tick hangs — how should it proceed?"
    )
    DeferredQuestion.record(question, session_id="")


def loop_admitted(name: str, now: dt.datetime) -> bool:
    """Whether *name* passes the unified enabled+due+reachable verdict right now.

    Reuses :func:`teatree.loops.loop_table.admitted_loop_names` scoped to the one
    loop, so the timer chain's admission can never drift from the tick's.
    """
    from teatree.loops.loop_table import admitted_loop_names  # noqa: PLC0415 — deferred: loaded at tick time

    return name in admitted_loop_names(now, only=name)


def _tick_argv(name: str) -> list[str]:
    """The subprocess argv for one per-loop tick — ``python -m teatree loops_tick --loop <name>``."""
    return [sys.executable, "-m", "teatree", "loops_tick", "--loop", name]


#: The process-group ids of every tick subprocess currently in flight, so the
#: worker's shutdown can SIGKILL any the executor-join timeout left orphaned. Keyed
#: by pgid (a session leader's pgid == its own pid). The tick runs in an executor
#: thread while the shutdown runs in the supervisor thread, so the set is lock-guarded.
_LIVE_TICK_PGIDS: set[int] = set()
_LIVE_TICK_LOCK = threading.Lock()


def _register_tick_pgid(pgid: int) -> None:
    with _LIVE_TICK_LOCK:
        _LIVE_TICK_PGIDS.add(pgid)


def _unregister_tick_pgid(pgid: int) -> None:
    with _LIVE_TICK_LOCK:
        _LIVE_TICK_PGIDS.discard(pgid)


def kill_live_tick_process_groups() -> list[int]:
    """SIGKILL every in-flight tick process group; return the pgids signalled.

    The worker's shutdown daemon-joins its executors with a short timeout but that
    join does not reach a tick subprocess: a kill-switch flip or a SIGTERM mid-tick
    tears down the executor thread that owned the deadline, orphaning the tick with
    no deadline owner (a no-zombie violation). This is called AFTER the join timeout
    so any still-running tick group is killed rather than left orphaned.
    """
    with _LIVE_TICK_LOCK:
        pgids = list(_LIVE_TICK_PGIDS)
    for pgid in pgids:
        _killpg(pgid)
        _unregister_tick_pgid(pgid)
    return pgids


def run_deadlined_tick(name: str, *, deadline: float) -> TickOutcome:
    """Run one per-loop tick as a deadlined subprocess in its OWN process group.

    ``python -m teatree loops_tick --loop <name>`` is spawned with
    ``start_new_session=True`` so it leads a fresh process group; on deadline expiry
    the WHOLE group is ``SIGKILL``-ed (the tick plus any grandchildren it spawned),
    so a hung tick can never outlive its deadline or strand children. The group is
    registered while it runs so the worker's shutdown can kill it too (see
    :func:`kill_live_tick_process_groups`). Standard over clever: a subprocess via
    ``python -m teatree`` isolates a crash/hang from the worker executor thread and
    gives an OS-level kill boundary an in-process ``call_command`` cannot.
    """
    proc = spawn_session_leader(_tick_argv(name), env={**os.environ, TICK_SUBPROCESS_ENV_MARKER: "1"})
    pgid = _tick_pgid(proc)
    if pgid is not None:
        _register_tick_pgid(pgid)
    try:
        returncode = proc.wait(timeout=deadline)
    except TimeoutExpired:
        _kill_process_group(proc)
        logger.warning("loop_timer %r tick exceeded its %.0fs deadline — killed the process group", name, deadline)
        return {"timed_out": True, "returncode": None}
    finally:
        if pgid is not None:
            _unregister_tick_pgid(pgid)
    return {"timed_out": False, "returncode": returncode}


def _tick_pgid(proc: Popen[str]) -> int | None:
    """The tick subprocess's own process-group id, or ``None`` if it already exited."""
    try:
        return os.getpgid(proc.pid)
    except ProcessLookupError:
        return None


def _killpg(pgid: int) -> None:
    """SIGKILL a whole process group; best-effort, never raise.

    Tolerates a group that is already gone (``ProcessLookupError``) and one whose
    leader's pid was recycled to a foreign-owned process (``PermissionError`` / EPERM)
    — in the shutdown sweep such a pgid is no longer our tick, and a single un-killable
    group must not abort killing the others.
    """
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return


def _kill_process_group(proc: Popen[str]) -> None:
    """SIGKILL the subprocess's whole process group and reap it, tolerating a dead child."""
    pgid = _tick_pgid(proc)
    if pgid is None:
        return
    _killpg(pgid)
    try:
        proc.wait(timeout=10)
    except TimeoutExpired:
        logger.exception("loop tick process group for pid %s did not die after SIGKILL", proc.pid)


def _outranked_by_running(running: "list[DBTaskResult]", *, my_id: str | uuid.UUID) -> bool:
    """Whether any RUNNING duplicate in *running* outranks this fire (lower id wins the tiebreak).

    Both this fire AND a concurrent duplicate are RUNNING rows; excluding this fire's
    own id, the lowest-id running timer survives and every other one dedups — so a slow
    anchor CAS that let a second executor claim a duplicate can no longer run two
    concurrent ticks (the READY-only self-dedup missed this). Exactly one winner: only
    ids strictly below mine count, so the minimum-id fire sees none. Both sides are
    normalized to the dashed-hex form so ``<`` is a stable total order regardless of the
    raw id spelling.
    """
    from django_tasks_db.models import normalize_uuid  # noqa: PLC0415 — deferred: heavy/optional dep at call site

    me = normalize_uuid(my_id)
    return any(normalize_uuid(row.id) < me for row in running)


@task(queue_name=LOOPS_QUEUE, takes_context=True)
def loop_timer(context: object, name: str) -> TimerResult:
    """One self-rescheduling loop-timer fire — the five-step tick body (#1796).

    See the module docstring for the step-by-step contract. The running row is
    already RUNNING (the worker claimed it before calling); step 1 dedups against a
    READY successor AND against any concurrently-RUNNING duplicate, excluding this
    fire's own id (``context.task_result.id``) so exactly one of two racing timers
    proceeds and the other collapses.
    """
    from django_tasks.base import TaskResultStatus  # noqa: PLC0415 — deferred: heavy/optional dep at call site

    from teatree.core.models import Loop  # noqa: PLC0415 — deferred: ORM import needs the app registry

    now = timezone.now()
    my_id = context.task_result.id  # ty: ignore[unresolved-attribute]  # django-tasks TaskContext

    # (0) kill-switch — the loop runner is OFF, so terminate the chain at its source:
    # do NOT re-enqueue a successor. The worker supervisor also stops on a flip-off, but
    # honouring the switch here means a timer claimed just before the flip cannot
    # perpetuate the chain, and neither can a stray inline drain of a loops-queue row.
    if not _loop_runner_enabled():
        return {"loop": name, "action": "halted"}

    # (1) self-dedup — a queued (READY) successor OR a lower-id concurrent RUNNING
    # duplicate already carries the chain. One query fetches both.
    live = _live_loop_timers(name)
    pending = [row for row in live if row.status == TaskResultStatus.READY]
    running = [row for row in live if row.status == TaskResultStatus.RUNNING]
    if pending or _outranked_by_running(running, my_id=my_id):
        return {"loop": name, "action": "deduped"}

    row = Loop.objects.filter(name=name).first()
    if row is None:
        # The loop was deleted; do not re-chain (the reconciler prunes stragglers).
        return {"loop": name, "action": "unknown"}

    # (2) successor-first re-enqueue — crash-safe, BEFORE any tick work. Floored so an
    # already-due successor at ``now`` cannot be claimed by a second executor and run
    # a duplicate tick subprocess while this tick is still in flight.
    enqueue_loop_timer(name, run_after=_idle_successor_run_after(row, now))

    # (3) admission — a held/disabled/not-due loop is a free no-op.
    if not loop_admitted(name, now):
        refine_successor(name, run_after=_idle_successor_run_after(row, now))
        return {"loop": name, "action": "skipped"}

    # (4) deadlined subprocess tick in its own process group.
    outcome = run_deadlined_tick(name, deadline=compute_tick_deadline(row))
    if outcome["timed_out"]:
        # The killed tick already consumed its anchor, so its work is lost until the
        # next slot (a full 24 h for a daily loop). Surface it loudly, never silent.
        _escalate_tick_timeout(name, deadline=compute_tick_deadline(row))

    # (5) post-tick refinement. A faulted tick (crash before the CAS, connector
    # outage, lost lease) leaves the anchor unmoved, so the loop is still "due" and
    # ``compute_successor_run_after`` would return ``now`` — an unbounded subprocess
    # hot-refire. Fall back to the idle floor when the anchor did NOT advance.
    fresh = Loop.objects.filter(name=name).first()
    if fresh is not None:
        anchor_advanced = fresh.last_run_at != row.last_run_at
        successor = compute_successor_run_after if anchor_advanced else _idle_successor_run_after
        refine_successor(name, run_after=successor(fresh, timezone.now()))

    return {
        "loop": name,
        "action": "ticked",
        "timed_out": outcome["timed_out"],
        "returncode": outcome["returncode"],
    }
