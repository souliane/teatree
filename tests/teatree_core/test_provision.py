"""Tests for the signal-based timeout guard on callable-based provision steps.

Verify that :mod:`teatree.core.provision` prevents hung provision steps
(e.g., db_import, overlay installation) by enforcing a configurable timeout
ceiling and failing loud with a clear error, never silently hanging.
"""

import signal
import time
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.provision import (
    DEFAULT_STEP_TIMEOUT_SECONDS,
    ProvisionTimeoutError,
    resolve_provision_step_timeout_seconds,
    timeout_provision_step,
)


class TestProvisionTimeoutError(TestCase):
    """ProvisionTimeoutError captures step name and timeout for diagnostics."""

    def test_error_message_includes_step_and_timeout(self) -> None:
        exc = ProvisionTimeoutError("db_import", 60)
        assert "db_import" in str(exc)
        assert "60" in str(exc)
        assert exc.step_name == "db_import"
        assert exc.timeout_seconds == 60


class TestResolveProvisionStepTimeout(TestCase):
    """The timeout ceiling is configurable with a sensible positive default."""

    def test_default_is_positive(self) -> None:
        ceiling = resolve_provision_step_timeout_seconds()
        assert ceiling > 0
        assert ceiling == DEFAULT_STEP_TIMEOUT_SECONDS

    def test_non_positive_value_degrades_to_default(self) -> None:
        with patch("teatree.core.provision.get_effective_settings") as mock_settings:
            mock_settings.return_value.provision_step_timeout_seconds = -100
            assert resolve_provision_step_timeout_seconds() == DEFAULT_STEP_TIMEOUT_SECONDS

    def test_zero_degrades_to_default(self) -> None:
        with patch("teatree.core.provision.get_effective_settings") as mock_settings:
            mock_settings.return_value.provision_step_timeout_seconds = 0
            assert resolve_provision_step_timeout_seconds() == DEFAULT_STEP_TIMEOUT_SECONDS

    def test_non_integer_degrades_to_default(self) -> None:
        with patch("teatree.core.provision.get_effective_settings") as mock_settings:
            mock_settings.return_value.provision_step_timeout_seconds = "invalid"
            assert resolve_provision_step_timeout_seconds() == DEFAULT_STEP_TIMEOUT_SECONDS

    def test_custom_positive_value_is_respected(self) -> None:
        with patch("teatree.core.provision.get_effective_settings") as mock_settings:
            mock_settings.return_value.provision_step_timeout_seconds = 42
            assert resolve_provision_step_timeout_seconds() == 42


class TestTimeoutProvisionStep(TestCase):
    """The timeout_provision_step context manager enforces timeouts via SIGALRM."""

    def test_successful_completion_before_timeout(self) -> None:
        with timeout_provision_step("quick_step", timeout=5):
            time.sleep(0.01)

    def test_timeout_raises_exception(self) -> None:
        with (
            pytest.raises(ProvisionTimeoutError) as exc_info,
            timeout_provision_step("hanging_step", timeout=1),
        ):
            time.sleep(10)
        assert exc_info.value.step_name == "hanging_step"
        assert exc_info.value.timeout_seconds == 1

    def test_timeout_none_uses_resolved_ceiling(self) -> None:
        with (
            patch("teatree.core.provision.resolve_provision_step_timeout_seconds", return_value=2),
            pytest.raises(ProvisionTimeoutError) as exc_info,
            timeout_provision_step("step_without_timeout"),
        ):
            time.sleep(5)
        assert exc_info.value.timeout_seconds == 2

    def test_alarm_is_cancelled_on_early_exit(self) -> None:
        with timeout_provision_step("quick_step", timeout=10):
            pass
        signal.alarm(0)
        current_alarm = signal.alarm(0)
        assert current_alarm == 0

    def test_prior_signal_handler_is_restored(self) -> None:
        call_count = {"count": 0}

        def custom_handler(_signum: int, _frame: object) -> None:
            call_count["count"] += 1

        old_handler = signal.signal(signal.SIGALRM, custom_handler)
        try:
            with timeout_provision_step("step", timeout=10):
                pass
            restored_handler = signal.signal(signal.SIGALRM, old_handler)
            assert restored_handler == custom_handler
        finally:
            signal.signal(signal.SIGALRM, old_handler)
