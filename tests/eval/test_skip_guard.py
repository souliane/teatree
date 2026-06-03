"""The all-skipped guard turns a decorative (collected>0, ran==0) run red."""

import pytest

from teatree.eval.skip_guard import AllSkippedError, assert_executed_when_required


class TestAssertExecutedWhenRequired:
    def test_collected_specs_all_skipped_raises_when_required(self) -> None:
        with pytest.raises(AllSkippedError) as exc:
            assert_executed_when_required(collected=123, executed=0, required=True)
        assert "123" in str(exc.value)

    def test_some_executed_does_not_raise_when_required(self) -> None:
        assert_executed_when_required(collected=123, executed=1, required=True)

    def test_all_executed_does_not_raise_when_required(self) -> None:
        assert_executed_when_required(collected=5, executed=5, required=True)

    def test_all_skipped_is_silent_when_not_required(self) -> None:
        assert_executed_when_required(collected=123, executed=0, required=False)

    def test_zero_collected_does_not_raise_when_required(self) -> None:
        assert_executed_when_required(collected=0, executed=0, required=True)

    def test_message_names_the_root_cause(self) -> None:
        with pytest.raises(AllSkippedError) as exc:
            assert_executed_when_required(collected=7, executed=0, required=True)
        message = str(exc.value)
        assert "7" in message
        assert "skipped" in message.lower()
        assert "claude" in message.lower() or "ANTHROPIC_API_KEY" in message
