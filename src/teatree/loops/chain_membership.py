"""Which loops should carry a ``loop_timer`` chain, and which admitted ones have no driver (#4185).

Chain MEMBERSHIP is the unified ENABLE verdict — hold > forced > preset >
``Loop.enabled`` — read in bulk through
:func:`teatree.loops.preset_status.effective_verdicts`, NOT the raw ``Loop.enabled``
column. That column is the verdict's LOWEST-precedence input, so a complete preset
decides every loop and the column is never reached: building the chain from it left
preset-admitted loops with no driver at all, and
:func:`teatree.loops.timer_reconciler.ensure_loop_timers` pruned away any timer they
did have.

Deliberately NOT :func:`teatree.loops.loop_table.admitted_loop_names`: that carries the
``is_due`` arm, which would prune the chain of every loop sitting between cadences. It
stays the per-FIRE admission (:func:`teatree.loops.timer_chains._loop_admitted`); this
module answers chain membership. The verdict also carries no colleague-facing arm, which
is right for membership — a colleague-facing loop keeps its chain through an away window
and step-3 admission skips its individual fires.
"""

from typing import TYPE_CHECKING

from teatree.request_cache import cached_per_request

if TYPE_CHECKING:
    from django_tasks_db.models import DBTaskResult


def loop_timers_by_name(status: str) -> "dict[str, list[DBTaskResult]]":
    """``loop_timer`` rows in *status*, grouped by the loop name they carry as their first arg."""
    from django_tasks_db.models import DBTaskResult  # noqa: PLC0415 — deferred: heavy/optional dep at call site

    from teatree.loops.timer_chains import _loop_timer_path  # noqa: PLC0415 — deferred: loaded at tick time

    grouped: dict[str, list[DBTaskResult]] = {}
    for row in DBTaskResult.objects.filter(task_path=_loop_timer_path(), status=status):
        args = row.args_kwargs.get("args") or []
        if args:
            grouped.setdefault(args[0], []).append(row)
    return grouped


@cached_per_request
def timer_chain_loop_names() -> set[str]:
    """The loops that should carry a timer chain: verdict-admitted, registered, and live-tick.

    Intersected with the registered mini-loops that are NOT ``off_live_tick`` — the heavy
    off-tick loops (``dream``, ``directive_loop``, ``outer_loop``) are driven by
    :mod:`teatree.loops.off_live_tick_driver` firing their own tick command, never a
    worker timer, so they never get a chain that would only ever no-op.
    """
    from teatree.loops.preset_status import effective_verdicts  # noqa: PLC0415 — deferred: ORM-backed resolver
    from teatree.loops.registry import iter_loops  # noqa: PLC0415 — deferred: loaded at tick time, not import

    registered = {loop.name for loop in iter_loops() if not loop.off_live_tick}
    admitted = {verdict.name for verdict in effective_verdicts() if verdict.admitted}
    return registered & admitted


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
    from django_tasks.base import TaskResultStatus  # noqa: PLC0415 — deferred: heavy/optional dep at call site
    from django_tasks_db.models import DBTaskResult  # noqa: PLC0415 — deferred: heavy/optional dep at call site

    from teatree.loops.timer_chains import _loop_timer_path  # noqa: PLC0415 — deferred: loaded at tick time

    live = DBTaskResult.objects.filter(
        task_path=_loop_timer_path(),
        status__in=(TaskResultStatus.READY, TaskResultStatus.RUNNING),
    ).values_list("args_kwargs", flat=True)
    return {args[0] for payload in live if (args := payload.get("args") or [])}


__all__ = ["driven_loop_names", "loop_timers_by_name", "starved_loop_names", "timer_chain_loop_names"]
