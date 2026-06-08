"""The all-skipped guard turns a decorative (collected>0, ran==0) run red."""

import pytest

from teatree.eval.skip_guard import (
    AllSkippedError,
    UnmeteredSdkRunError,
    assert_executed_when_required,
    assert_sdk_run_was_metered,
)


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


class TestAssertSdkRunWasMetered:
    """A metered (sdk) run that produced $0 of API cost never actually executed.

    This is the exact ``$0.00 (no metered calls)`` state the --bare auth bug
    produced: claude -p 'ran' but authenticated as nothing, so it made zero tool
    calls and metered zero cost. That must FAIL LOUD, never pass — the binding
    'fail loud, never skip-as-pass' rule for the metered path.
    """

    def test_zero_cost_executed_sdk_run_raises(self) -> None:
        with pytest.raises(UnmeteredSdkRunError) as exc:
            assert_sdk_run_was_metered(backend="sdk", executed=10, total_cost_usd=0.0)
        assert "metered" in str(exc.value).lower() or "$0" in str(exc.value)

    def test_metered_sdk_run_does_not_raise(self) -> None:
        assert_sdk_run_was_metered(backend="sdk", executed=10, total_cost_usd=0.0556)

    def test_subscription_backend_is_never_checked(self) -> None:
        # The free subscription lane is unmetered by design ($0 is correct).
        assert_sdk_run_was_metered(backend="subscription", executed=10, total_cost_usd=0.0)

    def test_zero_executed_sdk_run_is_left_to_the_all_skipped_guard(self) -> None:
        # executed==0 is the all-skipped guard's job; this guard only fires when
        # scenarios ran (executed>0) yet metered nothing — a different signal.
        assert_sdk_run_was_metered(backend="sdk", executed=0, total_cost_usd=0.0)
