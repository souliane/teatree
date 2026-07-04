"""No-bulk-close deterministic gate (PR-08, item 3)."""

from unittest.mock import patch

from teatree.config import UserSettings
from teatree.core.gates.bulk_close_gate import bulk_close_threshold, check_bulk_close


class TestUnderThreshold:
    def test_batch_at_threshold_allowed_without_tokens(self) -> None:
        assert check_bulk_close(items=[1, 2, 3], confirmed_tokens=[], threshold=3) == ""

    def test_batch_below_threshold_allowed(self) -> None:
        assert check_bulk_close(items=["a"], confirmed_tokens=[], threshold=5) == ""


class TestAboveThreshold:
    def test_refused_without_tokens(self) -> None:
        refusal = check_bulk_close(items=[1, 2, 3, 4], confirmed_tokens=[], threshold=3)
        assert "Refusing bulk close" in refusal
        assert "4 item(s) are un-confirmed" in refusal

    def test_refused_with_partial_tokens(self) -> None:
        refusal = check_bulk_close(items=[1, 2, 3, 4], confirmed_tokens=[1, 2, 3], threshold=3)
        assert "Refusing bulk close" in refusal
        # Only id 4 remains un-confirmed.
        assert "1 item(s) are un-confirmed" in refusal
        assert "4" in refusal

    def test_allowed_with_all_tokens(self) -> None:
        assert check_bulk_close(items=[1, 2, 3, 4], confirmed_tokens=[1, 2, 3, 4], threshold=3) == ""

    def test_tokens_are_string_normalized(self) -> None:
        # Ints and their string forms match after normalization.
        assert check_bulk_close(items=[1, 2, 3, 4], confirmed_tokens=[" 1 ", "2", "3", "4"], threshold=3) == ""


class TestThresholdResolution:
    def test_default_threshold_reads_setting(self) -> None:
        with patch(
            "teatree.core.gates.bulk_close_gate.get_effective_settings",
            return_value=UserSettings(bulk_close_threshold=2),
        ):
            assert bulk_close_threshold() == 2
            # 3 items > threshold 2 with no tokens → refused.
            assert check_bulk_close(items=[1, 2, 3], confirmed_tokens=[]) != ""
            # 2 items == threshold → allowed.
            assert check_bulk_close(items=[1, 2], confirmed_tokens=[]) == ""
