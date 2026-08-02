"""The pure Fibonacci step sequence and the minute schedule built on it (souliane/teatree#44, #2190)."""

from datetime import timedelta

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.modelkit.fibonacci import BACKOFF_BASE_MINUTES, fibonacci_minutes, fibonacci_step
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
