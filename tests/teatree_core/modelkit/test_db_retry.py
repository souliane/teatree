"""Transient-``database is locked`` resilience for the canonical-DB writes.

``souliane/teatree#1520``: under concurrent canonical-DB writers (the durable
loop's merge ceremony running alongside fix-agents) a transient
``OperationalError: database is locked`` could escape and abort the merge
keystone mid-flight. ``retry_on_locked`` wraps a DB-write callable in a bounded
retry-on-locked (exponential backoff, small cap) so a momentary lock blocks-
then-proceeds instead of crashing; a non-transient ``OperationalError`` and a
genuinely stuck lock still surface.

Only the unstoppable external — the clock (``time.sleep``) — is stubbed; the
retry control flow and the exception classification are exercised for real.
"""

from unittest.mock import patch

import pytest
from django.db import OperationalError

from teatree.core.modelkit.db_retry import retry_on_locked


class _Flaky:
    """A callable that raises ``database is locked`` for the first ``n`` calls."""

    def __init__(self, fail_times: int, *, exc: Exception | None = None) -> None:
        self.fail_times = fail_times
        self.exc = exc or OperationalError("database is locked")
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc
        return "ok"


class TestRetryOnLocked:
    def test_returns_value_without_retry_when_no_lock(self) -> None:
        flaky = _Flaky(fail_times=0)
        with patch("teatree.core.modelkit.db_retry.time.sleep") as sleep:
            assert retry_on_locked(flaky) == "ok"
        assert flaky.calls == 1
        sleep.assert_not_called()

    def test_retries_past_a_transient_lock_and_succeeds(self) -> None:
        flaky = _Flaky(fail_times=2)
        with patch("teatree.core.modelkit.db_retry.time.sleep") as sleep:
            assert retry_on_locked(flaky, attempts=5, base_delay=0.01) == "ok"
        assert flaky.calls == 3
        assert sleep.call_count == 2

    def test_backoff_is_exponential(self) -> None:
        flaky = _Flaky(fail_times=3)
        with patch("teatree.core.modelkit.db_retry.time.sleep") as sleep:
            retry_on_locked(flaky, attempts=5, base_delay=0.1)
        delays = [call.args[0] for call in sleep.call_args_list]
        assert delays == [0.1, 0.2, 0.4]

    def test_stuck_lock_surfaces_after_the_cap(self) -> None:
        flaky = _Flaky(fail_times=100)
        with (
            patch("teatree.core.modelkit.db_retry.time.sleep"),
            pytest.raises(OperationalError, match="database is locked"),
        ):
            retry_on_locked(flaky, attempts=3, base_delay=0.01)
        assert flaky.calls == 3

    def test_non_transient_operational_error_is_not_retried(self) -> None:
        flaky = _Flaky(fail_times=1, exc=OperationalError("no such table: teatree_merge_clear"))
        with (
            patch("teatree.core.modelkit.db_retry.time.sleep") as sleep,
            pytest.raises(OperationalError, match="no such table"),
        ):
            retry_on_locked(flaky, attempts=5, base_delay=0.01)
        assert flaky.calls == 1
        sleep.assert_not_called()

    def test_unrelated_exception_propagates_immediately(self) -> None:
        flaky = _Flaky(fail_times=1, exc=ValueError("boom"))
        with patch("teatree.core.modelkit.db_retry.time.sleep") as sleep, pytest.raises(ValueError, match="boom"):
            retry_on_locked(flaky, attempts=5, base_delay=0.01)
        assert flaky.calls == 1
        sleep.assert_not_called()
