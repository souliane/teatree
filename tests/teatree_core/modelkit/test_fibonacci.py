"""The pure Fibonacci step sequence and the minute schedule built on it (souliane/teatree#44, #2190)."""

from datetime import timedelta

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.modelkit.fibonacci import (
    BACKOFF_BASE_MINUTES,
    fibonacci_bump_index,
    fibonacci_minutes,
    fibonacci_step,
)
from teatree.core.models import LocalStackQueueItem, Ticket, Worktree


class TestFibonacciStep:
    """``fibonacci_step`` is the unitless sequence every backoff multiplies by."""

    @pytest.mark.parametrize(
        ("attempt", "expected"),
        [
            (0, 1),
            (1, 1),
            (2, 2),
            (3, 3),
            (4, 5),
            (5, 8),
            (6, 13),
            (7, 21),
            (8, 34),
            (9, 55),
        ],
    )
    def test_exact_sequence(self, attempt: int, expected: int) -> None:
        assert fibonacci_step(attempt) == expected

    def test_negative_attempt_clamps_to_the_first_step(self) -> None:
        assert fibonacci_step(-1) == 1
        assert fibonacci_step(-100) == 1

    def test_never_zero_or_negative(self) -> None:
        """A zero step would collapse any interval built on it to nothing."""
        for attempt in range(20):
            assert fibonacci_step(attempt) >= 1

    def test_minutes_are_the_base_times_the_step(self) -> None:
        for attempt in range(20):
            assert fibonacci_minutes(attempt) == BACKOFF_BASE_MINUTES * fibonacci_step(attempt)


class TestFibonacciMinutes:
    """``fibonacci_minutes`` returns the exact 1,1,2,3,5,8,13 schedule."""

    @pytest.mark.parametrize(
        ("attempt", "expected"),
        [
            (0, 1),
            (1, 1),
            (2, 2),
            (3, 3),
            (4, 5),
            (5, 8),
            (6, 13),
            (7, 21),
            (8, 34),
        ],
    )
    def test_exact_sequence(self, attempt: int, expected: int) -> None:
        assert fibonacci_minutes(attempt) == expected

    def test_negative_attempt_clamps_to_base(self) -> None:
        assert fibonacci_minutes(-1) == BACKOFF_BASE_MINUTES
        assert fibonacci_minutes(-100) == BACKOFF_BASE_MINUTES

    def test_base_is_one_minute(self) -> None:
        assert BACKOFF_BASE_MINUTES == 1

    def test_strictly_positive_for_all_attempts(self) -> None:
        """No step is ever 0 or negative — a zero wait would busy-loop the drainer."""
        for attempt in range(20):
            assert fibonacci_minutes(attempt) >= 1


class TestLocalStackQueueScheduleIsUnchanged(TestCase):
    """The queue drainer's every scheduled wait, pinned as literal minutes.

    ``fibonacci_minutes`` gained a second caller on a different base unit, so this
    walks the ONLY existing consumer across its whole ``max_attempts`` range and
    asserts the wall-clock gaps it produces — the schedule the drainer shipped
    with, expressed in the units the drainer stores, not in terms of the function
    under test.
    """

    _MINUTES_BY_ATTEMPT = (1, 2, 3, 5, 8, 13, 21, 34, 55, 89, 144, 233, 377)

    def test_every_attempt_waits_the_minutes_it_always_did(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://example.com/t3-heavy/issues/9401", overlay="t3-heavy")
        worktree = Worktree.objects.create(
            overlay="t3-heavy",
            ticket=ticket,
            repo_path="backend",
            branch="9401-feat",
            state=Worktree.State.PROVISIONED,
        )
        item = LocalStackQueueItem.objects.create(overlay=worktree.overlay, worktree=worktree)
        now = timezone.now()

        for expected in self._MINUTES_BY_ATTEMPT:
            item.schedule_next_attempt(error="full", now=now, max_attempts=len(self._MINUTES_BY_ATTEMPT))
            item.refresh_from_db()
            assert item.next_attempt_at == now + timedelta(minutes=expected)


class TestTheGapsGrowRatherThanRepeat:
    """A re-ask schedule where every gap is the same length nags a stale question forever.

    The bump index is derived from elapsed time alone — no stored counter — so a caller
    computes which bump is due from the row it already has, and the gaps widen without a
    cap: 1, 1, 2, 3, 5 … base units between bumps.
    """

    def test_no_gap_has_elapsed_before_the_first_one_completes(self) -> None:
        assert fibonacci_bump_index(0) == 0
        assert fibonacci_bump_index(0.9) == 0

    def test_each_completed_gap_advances_the_index_by_one(self) -> None:
        assert [fibonacci_bump_index(elapsed) for elapsed in (1, 2, 4, 7, 12, 20)] == [1, 2, 3, 4, 5, 6]

    def test_a_gap_is_held_until_it_fully_elapses(self) -> None:
        # The third gap is 2 units wide, so index 3 is not due at 3 units — only at 4.
        assert fibonacci_bump_index(3) == 2

    def test_the_schedule_is_uncapped(self) -> None:
        assert fibonacci_bump_index(1_000) > fibonacci_bump_index(100)

    def test_a_negative_elapsed_is_the_first_gap_rather_than_a_busy_loop(self) -> None:
        assert fibonacci_bump_index(-5) == 0
