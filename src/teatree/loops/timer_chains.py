"""Self-rescheduling loop-timer chains — durable, crash-surviving loop cadence (#1796).

Replaces the in-memory beat with django-tasks ``run_after`` rows: exactly one
pending ``loop_timer(name)`` task per verdict-admitted :class:`Loop` row (the
membership :func:`teatree.loops.chain_membership.timer_chain_loop_names` computes) on the
dedicated ``loops`` queue is a durable timer that survives a crash — the DB row IS the
scheduled fire. When a worker executor drains it (its ``run_after`` has elapsed)
the task runs a five-step body that re-schedules its own successor BEFORE doing
the tick work, so a crash mid-tick always leaves a queued successor and the chain
never stalls.

The tick body is gated by the FLEET verdict (step 0): a fire while the active preset
admits zero loops returns immediately without re-enqueueing a successor, so switching to
a stopping posture terminates the chain at its source (not only at the worker supervisor).
When the fleet admits work the five fixed steps run:

Step 1 — successor-first re-enqueue: schedule the next timer BEFORE the dedup and
before running the tick, so neither a crash during the tick nor a collapse into another
fire can leave the loop with nothing queued (crash-safe). A pending READY successor is
already that guarantee and gets no second row. The
``run_after`` is floored at ``now + IDLE_POLL_FLOOR_SECONDS``: an already-due
successor scheduled at ``now`` is immediately READY, so a second ``loops`` executor
claims it and spawns a duplicate tick subprocess while this one is still in flight —
the floor holds the successor back until this tick has moved the anchor. A future
interval/daily slot beyond the floor still fires at that slot; step 5 refines the
successor to the precise next slot once the tick's CAS moves the anchor.

Step 2 — self-dedup: a pending ``loop_timer`` for the same loop already carries the
chain, OR a LIVE concurrently-RUNNING duplicate with a lower id outranks this fire, so
this one stops (collapses duplicates to one — the "exactly one live timer per loop"
invariant self-heals; the id tiebreak lets exactly one of two racing RUNNING timers
proceed). A RUNNING row past its tick deadline is a corpse and never outranks anything:
deferring to a dead worker is how a chain was dropped while its loop kept a recent
anchor and read healthy (#4140).

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
import uuid
from typing import TYPE_CHECKING, TypedDict

from django.tasks import task
from django.utils import timezone

from teatree.loops.deadlined_tick import run_deadlined_tick
from teatree.loops.enable_verdict import fleet_admits_work

if TYPE_CHECKING:
    from django_tasks_db.models import DBTaskResult

    from teatree.core.models import Loop

logger = logging.getLogger(__name__)


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

#: A cadence-less (every-tick) loop has no interval, so its successor polls on this
#: floor rather than busy-spinning.
CADENCE_LESS_POLL_FLOOR_SECONDS = 60

#: A held/disabled/not-yet-due loop's successor is floored here so an idle chain
#: polls at a sane cadence instead of re-firing immediately.
IDLE_POLL_FLOOR_SECONDS = 60

#: The interval tick subprocess deadline is ``max(MIN_TICK_DEADLINE_SECONDS, 3 x cadence)``.
MIN_TICK_DEADLINE_SECONDS = 300.0
DEADLINE_CADENCE_MULTIPLIER = 3
#: ``3 x cadence`` is meaningless for a ``daily_at`` loop — the interval it would
#: multiply is the fallback the ``loop_script_requires_delay`` constraint forces every
#: script loop to carry, not the schedule the loop actually runs on. Daily ticks get
#: their own deadline; a genuine overrun past it escalates loudly.
DAILY_TICK_DEADLINE_SECONDS = 1800.0


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
    from django_tasks_db.models import DBTaskResult  # noqa: PLC0415 — deferred: Django import at call time

    rows = DBTaskResult.objects.filter(task_path=_loop_timer_path(), status=status)
    return [row for row in rows if row.args_kwargs.get("args") == [name]]


def pending_loop_timers(name: str) -> "list[DBTaskResult]":
    """READY (queued, not yet claimed) ``loop_timer`` rows for *name*."""
    from django.tasks import TaskResultStatus  # noqa: PLC0415 — deferred: Django import at call time

    return _timers_for(name, status=TaskResultStatus.READY)


def running_loop_timers(name: str) -> "list[DBTaskResult]":
    """RUNNING (claimed, executing) ``loop_timer`` rows for *name*."""
    from django.tasks import TaskResultStatus  # noqa: PLC0415 — deferred: Django import at call time

    return _timers_for(name, status=TaskResultStatus.RUNNING)


def _live_loop_timers(name: str) -> "list[DBTaskResult]":
    """READY-or-RUNNING ``loop_timer`` rows for *name* in ONE query.

    Step 1's self-dedup needs both the queued successor (READY) and any concurrent
    duplicate (RUNNING); fetching them together keeps the hot path at a single DB
    round-trip instead of two.
    """
    from django.tasks import TaskResultStatus  # noqa: PLC0415 — deferred: Django import at call time
    from django_tasks_db.models import DBTaskResult  # noqa: PLC0415 — deferred: Django import at call time

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
    from django.tasks import TaskResultStatus  # noqa: PLC0415 — deferred: Django import at call time
    from django_tasks_db.models import DBTaskResult  # noqa: PLC0415 — deferred: Django import at call time

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

    An interval loop gets ``max(300 s, 3 x cadence)``. ``daily_at`` OVERRIDES the
    interval (:meth:`Loop.is_due`), so a scheduled loop gets the dedicated
    :data:`DAILY_TICK_DEADLINE_SECONDS` whatever ``delay_seconds`` it also carries —
    keying that branch on an absent interval instead made it unreachable for every
    shipped daily loop, whose 86400 s fallback stretched the ceiling to 72 h.
    """
    if row.daily_at is not None:
        return DAILY_TICK_DEADLINE_SECONDS
    cadence = row.delay_seconds or 0
    return max(MIN_TICK_DEADLINE_SECONDS, DEADLINE_CADENCE_MULTIPLIER * float(cadence))


def _escalate_tick_timeout(name: str, *, deadline: float) -> None:
    """Record a durable escalation when a tick was SIGKILLed at its deadline, once per loop.

    A killed tick already consumed its cadence anchor (claimed BEFORE the scan in
    ``build_loop_table_jobs``), so this run's work is lost until the next slot — for a
    daily loop, a full 24 h, repeatable forever. That is exactly the "never silently
    freeze" invariant: the timeout must surface loudly, not sit behind a lone
    ``logger.warning``. Deduped through the sanctioned ``dedupe_marker`` seam, which
    collapses only OPEN (unanswered, undismissed) escalations — so a repeatedly-timing-
    out loop escalates once WHILE the question is pending, but a NEW timeout AFTER the
    user has answered/dismissed the last one re-escalates rather than being masked
    forever by the resolved row (F6.12).
    """
    from teatree.core.models.deferred_question import DeferredQuestion  # noqa: PLC0415 — deferred: ORM import

    marker = f"loop-tick-timeout loop={name}"
    question = (
        f"Loop {name!r} tick exceeded its {deadline:.0f}s deadline and was killed; its cadence "
        "anchor was already consumed, so this run's work is lost until the next slot. Raise the loop's "
        "deadline or investigate why the tick hangs — how should it proceed?"
    )
    DeferredQuestion.record(question, session_id="", dedupe_marker=marker)


def _loop_admitted(name: str, now: dt.datetime) -> bool:
    """Whether *name* passes the unified enabled+due+reachable verdict right now.

    Reuses :func:`teatree.loops.loop_table.admitted_loop_names` scoped to the one
    loop, so the timer chain's admission can never drift from the tick's.
    """
    from teatree.loops.loop_table import admitted_loop_names  # noqa: PLC0415 — deferred: loaded at tick time

    return name in admitted_loop_names(now, only=name)


def _outranked_by_running(
    running: "list[DBTaskResult]", *, my_id: str | uuid.UUID, loop_row: "Loop", now: dt.datetime
) -> bool:
    """Whether any LIVE running duplicate outranks this fire (lower id wins the tiebreak).

    Both this fire AND a concurrent duplicate are RUNNING rows; excluding this fire's
    own id, the lowest-id running timer survives and every other one dedups — so a slow
    anchor CAS that let a second executor claim a duplicate can no longer run two
    concurrent ticks (the READY-only self-dedup missed this). Exactly one winner: only
    ids strictly below mine count, so the minimum-id fire sees none. Both sides are
    normalized to the dashed-hex form so ``<`` is a stable total order regardless of the
    raw id spelling.

    A row past its tick deadline is a CORPSE, not a duplicate: its worker died holding
    it, so it will never reach the successor enqueue, and deferring to it hands the chain
    to something that cannot carry it (#4140). Corpses are disqualified through
    :func:`~teatree.loops.schedule_liveness.is_stranded` — the one predicate the reaper
    and the liveness alarm already share, so all three agree on which rows are dead.
    """
    from django_tasks_db.models import normalize_uuid  # noqa: PLC0415 — deferred: Django import at call time

    from teatree.loops.schedule_liveness import is_stranded  # noqa: PLC0415 — deferred: cycle-safe at call time

    me = normalize_uuid(my_id)
    live = (row for row in running if not is_stranded(row, loop_row, now))
    return any(normalize_uuid(row.id) < me for row in live)


@task(queue_name=LOOPS_QUEUE, takes_context=True)
def loop_timer(context: object, name: str) -> TimerResult:
    """One self-rescheduling loop-timer fire — the five-step tick body (#1796).

    See the module docstring for the step-by-step contract. The running row is
    already RUNNING (the worker claimed it before calling); step 1 dedups against a
    READY successor AND against any concurrently-RUNNING duplicate, excluding this
    fire's own id (``context.task_result.id``) so exactly one of two racing timers
    proceeds and the other collapses.
    """
    from django.tasks import TaskResultStatus  # noqa: PLC0415 — deferred: Django import at call time

    from teatree.core.models import Loop  # noqa: PLC0415 — deferred: ORM import needs the app registry

    now = timezone.now()
    my_id = context.task_result.id  # ty: ignore[unresolved-attribute]  # django-tasks TaskContext

    # (0) fleet verdict — the active preset admits nothing, so terminate the chain at its
    # source: do NOT re-enqueue a successor. The worker supervisor also quiesces on it, but
    # honouring it here means a timer claimed just before the switch cannot perpetuate the
    # chain, and neither can a stray inline drain of a loops-queue row.
    if not fleet_admits_work(now):
        return {"loop": name, "action": "halted"}

    live = _live_loop_timers(name)
    pending = [row for row in live if row.status == TaskResultStatus.READY]
    running = [row for row in live if row.status == TaskResultStatus.RUNNING]

    row = Loop.objects.filter(name=name).first()
    if row is None:
        # The loop was deleted; do not re-chain (the reconciler prunes stragglers).
        return {"loop": name, "action": "unknown"}

    # (1) successor-first re-enqueue — crash-safe, BEFORE the dedup and before any tick
    # work. Floored so an already-due successor at ``now`` cannot be claimed by a second
    # executor and run a duplicate tick subprocess while this tick is still in flight.
    # It precedes the dedup because collapsing into another fire is a BET that the other
    # fire will carry the chain, and a fire that returns before enqueuing a successor
    # loses that bet silently — the loop keeps a recent anchor and never fires again
    # (#4140). A READY successor is already the guarantee, so it needs no second row.
    if not pending:
        enqueue_loop_timer(name, run_after=_idle_successor_run_after(row, now))

    # (2) self-dedup — a queued (READY) successor OR a lower-id LIVE concurrent RUNNING
    # duplicate already carries the chain, and this fire has guaranteed one either way.
    if pending or _outranked_by_running(running, my_id=my_id, loop_row=row, now=now):
        return {"loop": name, "action": "deduped"}

    # (3) admission — a held/disabled/not-due loop is a free no-op.
    if not _loop_admitted(name, now):
        refine_successor(name, run_after=_idle_successor_run_after(row, now))
        return {"loop": name, "action": "skipped"}

    # (4) deadlined subprocess tick in its own process group.
    outcome = run_deadlined_tick(name, deadline=compute_tick_deadline(row))
    if outcome["timed_out"]:
        # The killed tick already consumed its anchor, so its work is lost until the
        # next slot (a full 24 h for a daily loop). Surface it loudly, never silent.
        _escalate_tick_timeout(name, deadline=compute_tick_deadline(row))
    elif outcome["returncode"]:
        # The chain survives a failing tick by design; without this the only record of
        # a loop failing every slot is a return value no health surface reads.
        logger.warning("loop_timer %r tick exited %s — the loop did not do its work", name, outcome["returncode"])

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
