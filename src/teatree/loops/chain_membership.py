"""Which loops should carry a ``loop_timer`` chain, and which admitted ones have no driver (#4185).

Chain MEMBERSHIP is the unified ENABLE verdict — hold > manual override > preset —
read in bulk through :func:`teatree.loops.enable_verdict.effective_verdicts`, the SAME
:class:`~teatree.loops.enable_verdict.EnablePlanes` seam the live tick's per-fire
admission gates on. NOT the raw ``Loop.enabled`` column: that is the MANUAL-override
layer, empty on a fleet nobody has intervened on, so the preset decides every loop and
the column answers about none of them. Building the chain from it left preset-admitted
loops with no driver at all while
:func:`teatree.loops.timer_reconciler.ensure_loop_timers` pruned away any timer they had.

Membership is deliberately WIDER than the per-fire admission, but never
differently-sourced. :func:`teatree.loops.loop_table.admitted_loop_names` narrows the
same seam with the arm this module must not carry: ``is_due``, which would prune the
chain of every loop sitting between cadences.
"""

from typing import TYPE_CHECKING

from teatree.request_cache import cached_per_request

if TYPE_CHECKING:
    import datetime as dt

    from django_tasks_db.models import DBTaskResult


def loop_timers_by_name(status: str) -> "dict[str, list[DBTaskResult]]":
    """``loop_timer`` rows in *status*, grouped by the loop name they carry as their first arg."""
    from django_tasks_db.models import DBTaskResult  # noqa: PLC0415 — deferred: Django import at call time

    from teatree.loops.timer_chains import _loop_timer_path  # noqa: PLC0415 — deferred: loaded at tick time

    grouped: dict[str, list[DBTaskResult]] = {}
    for row in DBTaskResult.objects.filter(task_path=_loop_timer_path(), status=status):
        args = row.args_kwargs.get("args") or []
        if args:
            grouped.setdefault(args[0], []).append(row)
    return grouped


def live_tick_loop_names() -> set[str]:
    """Registered mini-loops the live tick drives — an ``off_live_tick`` row has its own command.

    Membership's registry half on its own, for the one caller that needs the
    ``off_live_tick`` cut WITHOUT the enable verdict: the staleness alarm also measures the
    loops the OPERATOR left on, so a mode that masks every one of them off reads as a
    STOPPED fleet rather than an empty one.
    """
    from teatree.loops.registry import iter_loops  # noqa: PLC0415 — deferred: loaded at tick time, not import

    return {loop.name for loop in iter_loops() if not loop.off_live_tick}


@cached_per_request
def timer_chain_loop_names(now: "dt.datetime | None" = None) -> set[str]:
    """The loops that should carry a timer chain: verdict-admitted, registered, and live-tick.

    Intersected with the registered mini-loops that are NOT ``off_live_tick`` — the heavy
    off-tick loops (``dream``, ``directive_loop``, ``outer_loop``) are driven by
    :mod:`teatree.loops.off_live_tick_driver` firing their own tick command, never a
    worker timer, so they never get a chain that would only ever no-op.

    Membership is the enable verdict itself
    (:func:`teatree.loops.enable_verdict.membership_loop_names`) — the SAME object the
    tick gates a fire on. Every arm of that verdict moves only on a durable write or a
    schedule boundary, so a chain built now is still answerable when it fires.

    *now* pins the instant the mode is resolved at, so a caller that judges membership
    ALONGSIDE another mode-derived reading (the staleness alarm's suppression arm) asks
    both questions of the same moment rather than of two ``timezone.now()`` calls.
    """
    from teatree.loops.enable_verdict import membership_loop_names  # noqa: PLC0415 — deferred: ORM-backed resolver

    return live_tick_loop_names() & membership_loop_names(now)


def starved_loop_names() -> set[str]:
    """Admitted loops with no live ``loop_timer`` row at all — admitted but driverless.

    DERIVED, never stored: chain membership minus every loop carrying a READY or RUNNING
    timer. Post-fix the state is transient (at most one reconcile interval) or a
    regression canary — a PERSISTENT ``starved`` reading means the reconcile chain itself
    has stopped, which is exactly what a canary should catch.
    """
    return timer_chain_loop_names() - driven_loop_names()


@cached_per_request
def driven_loop_names() -> set[str]:
    """Every loop carrying a READY or RUNNING ``loop_timer`` row — one query, both statuses.

    The dash polls the pages that read this, so the two statuses are one ``status__in``
    read rather than two round trips.
    """
    from django.tasks import TaskResultStatus  # noqa: PLC0415 — deferred: Django import at call time
    from django_tasks_db.models import DBTaskResult  # noqa: PLC0415 — deferred: Django import at call time

    from teatree.loops.timer_chains import _loop_timer_path  # noqa: PLC0415 — deferred: loaded at tick time

    live = DBTaskResult.objects.filter(
        task_path=_loop_timer_path(),
        status__in=(TaskResultStatus.READY, TaskResultStatus.RUNNING),
    ).values_list("args_kwargs", flat=True)
    return {args[0] for payload in live if (args := payload.get("args") or [])}


__all__ = [
    "driven_loop_names",
    "live_tick_loop_names",
    "loop_timers_by_name",
    "starved_loop_names",
    "timer_chain_loop_names",
]
