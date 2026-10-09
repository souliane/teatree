"""Loop watchdog + per-run usage sampling for the executor (#882).

The stuck-loop / cost-spike detector (:class:`LoopWatchdog`) and the
accumulated ``TaskAttempt`` delta snapshot (:class:`TaskUsage`) it evaluates,
factored out of :mod:`teatree.agents.runner` so the driver stays focused on
the harness lifecycle. :func:`_sample_usage_closing_connection` is the worker-
thread sampler the heartbeat loop offloads the aggregate read to.
"""

from dataclasses import dataclass

from django.db.models import Sum

from teatree.config import get_effective_settings
from teatree.core.models import Task
from teatree.utils.thread_db import close_thread_db_connections


@dataclass(frozen=True)
class TaskUsage:
    """Accumulated ``TaskAttempt`` deltas for one task.

    Sampled once on the main thread before the agent starts: ``num_turns`` /
    ``cost_usd`` only land in the DB *after* an attempt completes, so
    prior-attempt totals are static for the current run.
    """

    turns: int
    cost_usd: float

    @classmethod
    def for_task(cls, task: Task) -> "TaskUsage":
        attempts = task.attempts
        totals = attempts.aggregate(turns=Sum("num_turns"), cost=Sum("cost_usd"))
        return cls(turns=totals["turns"] or 0, cost_usd=totals["cost"] or 0.0)


@dataclass(frozen=True)
class LoopWatchdog:
    """Detects a stuck loop / cost spike during the heartbeat loop (#882).

    Evaluates the running task's wall-clock runtime plus the accumulated
    ``TaskAttempt.num_turns`` / ``cost_usd`` deltas. When a ceiling is
    crossed the heartbeat loop interrupts the agent and a ``stuck_loop``
    ``TaskAttempt`` failure is recorded with the observed deltas. A ceiling
    of ``0`` disables that dimension.
    """

    max_runtime_seconds: float
    max_turns: int
    max_cost_usd: float

    @classmethod
    def from_settings(cls) -> "LoopWatchdog":
        """Build the watchdog from effective DB-home config."""
        effective = get_effective_settings()
        return cls(
            max_runtime_seconds=float(effective.watchdog_max_runtime_seconds),
            max_turns=int(effective.watchdog_max_turns),
            max_cost_usd=float(effective.watchdog_max_cost_usd),
        )

    def breach_reason(self, task: Task, *, elapsed_seconds: float, usage: TaskUsage | None = None) -> str | None:
        """Return a reason string with observed deltas, or ``None`` if healthy.

        *usage* is the pre-sampled accumulated delta snapshot; when omitted
        it is read from *task* (convenience for callers outside the loop).
        """
        if self.max_runtime_seconds and elapsed_seconds > self.max_runtime_seconds:
            return (
                f"runtime ceiling exceeded: ran {elapsed_seconds:.0f}s "
                f"> {self.max_runtime_seconds:.0f}s without exiting"
            )
        if self.max_turns or self.max_cost_usd:
            if usage is None:
                usage = TaskUsage.for_task(task)
            if self.max_turns and usage.turns > self.max_turns:
                return f"turns ceiling exceeded: {usage.turns} turns > {self.max_turns} without progress"
            if self.max_cost_usd and usage.cost_usd > self.max_cost_usd:
                return f"cost ceiling exceeded: ${usage.cost_usd:.2f} > ${self.max_cost_usd:.2f} without progress"
        return None


def _sample_usage_closing_connection(task: Task) -> TaskUsage:
    """Sample :meth:`TaskUsage.for_task` and close THIS thread's DB connection.

    Run as an :func:`asyncio.to_thread` worker: the aggregate query opens a
    Django connection bound to the worker thread, which never closes itself.
    Neither ``close_old_connections`` nor ``connection.close()`` reaps it —
    the former only closes connections past ``CONN_MAX_AGE`` / marked unusable,
    and the latter is a documented no-op on an in-memory database. The raw
    DB-API handle has to be closed directly, which is what
    :func:`~teatree.utils.thread_db.close_thread_db_connections` does;
    otherwise the handle outlives the thread and surfaces as a
    ``ResourceWarning: unclosed database`` when the thread is GC'd (an
    order-dependent test flake, and a real connection leak in production).
    """
    try:
        return TaskUsage.for_task(task)
    finally:
        close_thread_db_connections()
