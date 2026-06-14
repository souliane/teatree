"""The pure Fibonacci-minute backoff schedule (souliane/teatree#44, #2190)."""

import pytest

from teatree.core.modelkit.fibonacci import BACKOFF_BASE_MINUTES, fibonacci_minutes


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
