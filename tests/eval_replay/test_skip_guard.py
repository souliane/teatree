"""The all-skipped guard turns a decorative (collected>0, ran==0) run red."""

import pytest

from teatree.eval.skip_guard import (
    AllSkippedError,
    UnmeteredApiRunError,
    UnmeteredJudgeError,
    assert_api_run_was_metered,
    assert_executed_when_required,
    assert_judge_was_metered,
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
    """A metered (api) run that produced $0 of API cost never actually executed.

    This is the exact ``$0.00 (no metered calls)`` state the --bare auth bug
    produced: claude -p 'ran' but authenticated as nothing, so it made zero tool
    calls and metered zero cost. That must FAIL LOUD, never pass — the binding
    'fail loud, never skip-as-pass' rule for the metered path.
    """

    def test_zero_cost_executed_api_run_raises(self) -> None:
        with pytest.raises(UnmeteredApiRunError) as exc:
            assert_api_run_was_metered(backend="api", executed=10, total_cost_usd=0.0)
        assert "metered" in str(exc.value).lower() or "$0" in str(exc.value)

    def test_unmetered_message_names_the_oauth_window_throttle_cause(self) -> None:
        # The DEFAULT eval lane runs on subscription OAuth (T3_EVAL_CREDENTIAL=
        # subscription_oauth, the #2707 REVERSAL), whose depleting 5h/7d usage
        # window throttles a full run to $0. The message must name that cause so a
        # throttled run is NOT misdiagnosed as a missing API key (the old message
        # named ANTHROPIC_API_KEY as the sole cause, misreading every OAuth
        # throttle). The key-absent case — the only cause on a metered_api_key run
        # — is still surfaced as the secondary cause.
        with pytest.raises(UnmeteredApiRunError) as exc:
            assert_api_run_was_metered(backend="api", executed=10, total_cost_usd=0.0)
        message = str(exc.value).lower()
        assert "oauth" in message
        assert "usage window" in message
        assert "throttl" in message
        assert "key-absent" in message or "credential" in message

    def test_metered_api_run_does_not_raise(self) -> None:
        assert_api_run_was_metered(backend="api", executed=10, total_cost_usd=0.0556)

    def test_transcript_backend_is_never_checked(self) -> None:
        # The transcript lane runs no model by design ($0 is correct).
        assert_api_run_was_metered(backend="transcript", executed=10, total_cost_usd=0.0)

    def test_zero_executed_api_run_is_left_to_the_all_skipped_guard(self) -> None:
        # executed==0 is the all-skipped guard's job; this guard only fires when
        # scenarios ran (executed>0) yet metered nothing — a different signal.
        assert_api_run_was_metered(backend="api", executed=0, total_cost_usd=0.0)


class TestAssertJudgeWasMetered:
    """A judge oracle whose every judge call skipped never actually graded.

    Judge spend flows through a separate ``claude_agent_sdk.query`` that is never
    folded into ``run.cost_usd``, so the api-metered guard cannot see it. A
    ``--judge`` run whose judge-oracle scenarios all skipped (claude absent)
    exits GREEN having judged nothing — a fake-green this guard turns RED.
    """

    def test_judge_requested_but_no_judge_calls_raises(self) -> None:
        # MUST-FIRE: the judge was asked for, an oracle scenario was graded, yet
        # every judge call skipped — nothing was actually judged.
        with pytest.raises(UnmeteredJudgeError) as exc:
            assert_judge_was_metered(judge_requested=True, judge_eligible=3, judge_calls=0)
        assert "judge" in str(exc.value).lower()

    def test_judge_not_requested_never_raises(self) -> None:
        # MUST-NOT-FIRE: no `--judge` — a matcher-only run has no judge to meter.
        assert_judge_was_metered(judge_requested=False, judge_eligible=0, judge_calls=0)

    def test_no_judge_oracle_scenarios_never_raises(self) -> None:
        # MUST-NOT-FIRE: `--judge` set but no scenario carries a judge block, so
        # zero judge calls is correct, not a fake-green.
        assert_judge_was_metered(judge_requested=True, judge_eligible=0, judge_calls=0)

    def test_at_least_one_judge_call_does_not_raise(self) -> None:
        # MUST-NOT-FIRE: the judge actually graded an oracle scenario.
        assert_judge_was_metered(judge_requested=True, judge_eligible=3, judge_calls=1)

    def test_message_names_the_judge_skip_cause(self) -> None:
        with pytest.raises(UnmeteredJudgeError) as exc:
            assert_judge_was_metered(judge_requested=True, judge_eligible=1, judge_calls=0)
        message = str(exc.value).lower()
        assert "judge" in message
        assert "claude" in message or "skipped" in message
