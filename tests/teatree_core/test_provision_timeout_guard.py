"""Tests for provision step timeout guard (#TODO-7).

Verifies that individual provision steps are bounded by a configurable timeout
that prevents indefinite hangs during stalled operations.
"""

import signal
import time
from unittest.mock import MagicMock, patch

import pytest

from teatree.core.provision import (
    DEFAULT_PROVISION_STEP_TIMEOUT_SECONDS,
    ProvisionTimeoutError,
    resolve_provision_step_timeout_seconds,
    timeout_provision_step,
)


class TestProvisionTimeout:
    def test_exception_stores_step_name_and_timeout(self) -> None:
        exc = ProvisionTimeoutError("db-import", 300)
        assert exc.step_name == "db-import"
        assert exc.timeout_seconds == 300
        assert "db-import" in str(exc)
        assert "300s" in str(exc)

    def test_exception_message_is_clear(self) -> None:
        exc = ProvisionTimeoutError("migrations", 600)
        msg = str(exc)
        assert "migrations" in msg
        assert "600" in msg
        assert "timed out" in msg.lower()


class TestResolveTimeoutSeconds:
    def test_default_timeout_when_not_configured(self) -> None:
        with patch("teatree.core.provision.get_effective_settings") as mock_settings:
            mock_settings.return_value = MagicMock(spec=[])
            timeout = resolve_provision_step_timeout_seconds()
            assert timeout == DEFAULT_PROVISION_STEP_TIMEOUT_SECONDS

    def test_reads_configured_timeout(self) -> None:
        with patch("teatree.core.provision.get_effective_settings") as mock_settings:
            mock_settings.return_value = MagicMock(provision_step_timeout_seconds=120)
            timeout = resolve_provision_step_timeout_seconds()
            assert timeout == 120

    def test_degraded_on_non_positive_value(self) -> None:
        with patch("teatree.core.provision.get_effective_settings") as mock_settings:
            mock_settings.return_value = MagicMock(provision_step_timeout_seconds=0)
            timeout = resolve_provision_step_timeout_seconds()
            assert timeout == DEFAULT_PROVISION_STEP_TIMEOUT_SECONDS

    def test_degraded_on_non_integer_value(self) -> None:
        with patch("teatree.core.provision.get_effective_settings") as mock_settings:
            mock_settings.return_value = MagicMock(provision_step_timeout_seconds="invalid")
            timeout = resolve_provision_step_timeout_seconds()
            assert timeout == DEFAULT_PROVISION_STEP_TIMEOUT_SECONDS


class TestTimeoutProvisionStep:
    def test_completes_normally_when_step_finishes_before_timeout(self) -> None:
        with timeout_provision_step("fast-step", timeout_seconds=2):
            time.sleep(0.1)

    def test_raises_timeout_on_stalled_step(self) -> None:
        with (
            pytest.raises(ProvisionTimeoutError) as exc_info,
            timeout_provision_step("stalled-step", timeout_seconds=1),
        ):
            time.sleep(3)
        assert exc_info.value.step_name == "stalled-step"
        assert exc_info.value.timeout_seconds == 1

    def test_timeout_names_the_step(self) -> None:
        with pytest.raises(ProvisionTimeoutError) as exc_info, timeout_provision_step("db-import", timeout_seconds=1):
            time.sleep(2)
        assert "db-import" in str(exc_info.value)

    def test_uses_configured_timeout_when_not_supplied(self) -> None:
        with (
            patch("teatree.core.provision.resolve_provision_step_timeout_seconds", return_value=1),
            pytest.raises(ProvisionTimeoutError),
            timeout_provision_step("step"),
        ):
            time.sleep(2)

    def test_restores_previous_signal_handler(self) -> None:
        original_handler = signal.signal(signal.SIGALRM, signal.SIG_DFL)
        signal.signal(signal.SIGALRM, original_handler)

        try:
            with timeout_provision_step("step", timeout_seconds=10):
                pass
        finally:
            final_handler = signal.signal(signal.SIGALRM, signal.SIG_DFL)
            assert final_handler == original_handler
