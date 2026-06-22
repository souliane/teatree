"""Tests for the provision step timeout-retry guard (#TODO-7).

A single-shot timeout aborts a provision step the first time a DB query runs
long, blocking all downstream work even when a retry would have succeeded. The
retry guard re-runs the step with exponential backoff and only fails — loud,
never silent — once the attempts are exhausted.
"""

import time
from unittest.mock import patch

import pytest

from teatree.core.provision import (
    DEFAULT_PROVISION_RETRY_ATTEMPTS,
    DEFAULT_PROVISION_RETRY_BASE_DELAY,
    ProvisionTimeoutError,
    retry_provision_step,
)


class TestRetryProvisionStep:
    def test_returns_operation_result_on_first_success(self) -> None:
        result = retry_provision_step("fast", lambda: "ok", step_timeout_seconds=2)
        assert result == "ok"

    def test_runs_operation_exactly_once_when_it_succeeds(self) -> None:
        calls: list[int] = []

        def op() -> int:
            calls.append(1)
            return 7

        result = retry_provision_step("once", op, step_timeout_seconds=2)
        assert result == 7
        assert len(calls) == 1

    def test_retries_after_a_transient_timeout_then_succeeds(self) -> None:
        attempts: list[int] = []

        def op() -> str:
            attempts.append(1)
            if len(attempts) == 1:
                time.sleep(2)
            return "recovered"

        with patch("teatree.core.provision._backoff_sleep") as sleep:
            result = retry_provision_step(
                "db-query",
                op,
                step_timeout_seconds=1,
                attempts=3,
                base_delay=0.01,
            )
        assert result == "recovered"
        assert len(attempts) == 2
        assert sleep.called

    def test_fails_safe_when_attempts_exhausted(self) -> None:
        def always_hang() -> None:
            time.sleep(5)

        with (
            patch("teatree.core.provision._backoff_sleep"),
            pytest.raises(ProvisionTimeoutError) as exc_info,
        ):
            retry_provision_step(
                "db-import",
                always_hang,
                step_timeout_seconds=1,
                attempts=2,
                base_delay=0.01,
            )
        assert exc_info.value.step_name == "db-import"

    def test_backoff_is_exponential(self) -> None:
        def always_hang() -> None:
            time.sleep(5)

        with patch("teatree.core.provision._backoff_sleep") as sleep, pytest.raises(ProvisionTimeoutError):
            retry_provision_step(
                "step",
                always_hang,
                step_timeout_seconds=1,
                attempts=4,
                base_delay=0.1,
            )
        delays = [call.args[0] for call in sleep.call_args_list]
        assert delays == [0.1, 0.2, 0.4]

    def test_non_timeout_error_propagates_without_retry(self) -> None:
        attempts: list[int] = []
        boom_msg = "not a timeout"

        def boom() -> None:
            attempts.append(1)
            raise ValueError(boom_msg)

        with pytest.raises(ValueError, match="not a timeout"):
            retry_provision_step("boom", boom, step_timeout_seconds=2, attempts=3, base_delay=0.01)
        assert len(attempts) == 1

    def test_defaults_are_positive(self) -> None:
        assert DEFAULT_PROVISION_RETRY_ATTEMPTS > 0
        assert DEFAULT_PROVISION_RETRY_BASE_DELAY > 0
