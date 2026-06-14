"""Pure Fibonacci-minute backoff schedule (souliane/teatree#44, #2190).

The local-stack acquisition queue (``core/gates/local_stack_gate.py`` →
``LocalStackQueueItem``) retries a stalled ``worktree start`` / ``workspace
start`` with a Fibonacci-minute gap between attempts: 1, 1, 2, 3, 5, 8, 13…
A queued request never tears down another ticket's stack — it waits for a
slot to free naturally (a reap, a teardown) and backs off geometrically so a
permanently-full host does not re-shell docker every tick.

The base unit is one minute (a constant), so ``fibonacci_minutes(attempt)``
returns the Fibonacci number for the attempt index directly. Kept pure (no
DB, no clock) so it is exhaustively unit-testable and so the queue model can
compute ``next_attempt_at`` from ``attempt_count`` deterministically.
"""

#: Backoff base in minutes. The schedule is ``base * fib(attempt)``; with the
#: base held at one minute the minute count IS the Fibonacci number.
BACKOFF_BASE_MINUTES = 1


def fibonacci_minutes(attempt: int) -> int:
    """Return the backoff in minutes for a zero-based *attempt* index.

    The schedule is the Fibonacci sequence starting ``1, 1, 2, 3, 5, 8, 13``:
    ``fibonacci_minutes(0) == 1``, ``fibonacci_minutes(1) == 1``,
    ``fibonacci_minutes(2) == 2`` … A negative *attempt* clamps to the first
    step (1 minute) so a malformed caller never produces a zero or negative
    wait that would busy-loop the drainer.
    """
    if attempt <= 0:
        return BACKOFF_BASE_MINUTES
    prev, current = 1, 1
    for _ in range(attempt):
        prev, current = current, prev + current
    return BACKOFF_BASE_MINUTES * prev


__all__ = ["BACKOFF_BASE_MINUTES", "fibonacci_minutes"]
