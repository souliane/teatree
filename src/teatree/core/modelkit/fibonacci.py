"""Pure Fibonacci backoff schedule, in steps and in minutes (souliane/teatree#44, #2190).

:func:`fibonacci_step` is the unitless sequence ``1, 1, 2, 3, 5, 8, 13…``; a
caller multiplies it by whatever base its own cadence is measured in. Two do:

- the local-stack acquisition queue (``core/gates/local_stack_gate.py`` →
  ``LocalStackQueueItem``) retries a stalled ``worktree start`` / ``workspace
  start`` on :func:`fibonacci_minutes` — one MINUTE per step. A queued request
  never tears down another ticket's stack; it waits for a slot to free
  naturally and backs off geometrically so a permanently-full host does not
  re-shell docker every tick.
- the review nag (``core/review/mr_triage.TriageThresholds``) re-asks on the
  same steps against a per-owner base measured in DAYS, capped.

Kept pure (no DB, no clock) so it is exhaustively unit-testable and so each
caller can compute its next-attempt moment from an attempt count
deterministically.
"""

#: Backoff base in minutes for the local-stack queue. Its schedule is
#: ``base * fibonacci_step(attempt)``; with the base held at one minute the
#: minute count IS the Fibonacci number.
BACKOFF_BASE_MINUTES = 1


def fibonacci_step(attempt: int) -> int:
    """Return the Fibonacci multiplier for a zero-based *attempt* index.

    ``fibonacci_step(0) == 1``, ``fibonacci_step(1) == 1``,
    ``fibonacci_step(2) == 2`` … A negative *attempt* clamps to the first step
    so a malformed caller never produces a zero or negative multiplier, which
    would collapse its interval to nothing and busy-loop whatever consumes it.
    """
    if attempt <= 0:
        return 1
    prev, current = 1, 1
    for _ in range(attempt):
        prev, current = current, prev + current
    return prev


def fibonacci_minutes(attempt: int) -> int:
    """Return the local-stack queue backoff in minutes for a zero-based *attempt*."""
    return BACKOFF_BASE_MINUTES * fibonacci_step(attempt)


__all__ = ["BACKOFF_BASE_MINUTES", "fibonacci_minutes", "fibonacci_step"]
