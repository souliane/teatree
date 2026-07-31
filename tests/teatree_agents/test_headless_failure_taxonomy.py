"""The terminal-``ResultMessage`` failure taxonomy the headless driver folds through.

Two pure classifiers with one job each: decide whether a run was stopped by a
model-access limit (:func:`limit_match`), and otherwise describe a run that did
not complete cleanly so it is recorded rather than laundered into a completion
(:func:`error_result_reason`). Both are pure functions of the SDK message, so a
verdict is reproducible without a task, a harness, or a database.
"""

from claude_agent_sdk import ResultMessage
from claude_agent_sdk.types import RateLimitInfo

from teatree.agents.headless_failure_taxonomy import RESULT_ERROR_PREFIX, error_result_reason, limit_match
from teatree.llm.anthropic_limits import LimitCause


def _result(*, is_error: bool = False, subtype: str = "success", result: str = "", errors=None) -> ResultMessage:
    return ResultMessage(
        subtype=subtype,
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=1,
        session_id="s1",
        result=result or None,
        errors=errors,
    )


class TestErrorResultReason:
    def test_a_clean_run_has_no_failure_reason(self) -> None:
        assert error_result_reason(_result()) is None

    def test_a_missing_terminal_message_is_a_failure(self) -> None:
        reason = error_result_reason(None)
        assert reason is not None
        assert reason.startswith(RESULT_ERROR_PREFIX)

    def test_the_reason_carries_the_cli_s_own_diagnosis(self) -> None:
        reason = error_result_reason(_result(is_error=True, subtype="error_during_execution", result="boom"))
        assert reason is not None
        assert "subtype=error_during_execution" in reason
        assert "boom" in reason

    def test_the_errors_list_stands_in_when_there_is_no_result_text(self) -> None:
        reason = error_result_reason(_result(is_error=True, subtype="error", errors=["first", "second"]))
        assert reason is not None
        assert "first; second" in reason


class TestLimitMatch:
    def test_a_healthy_result_is_never_a_limit(self) -> None:
        # A run that merely DISCUSSES limits in its text is not a limit hit.
        assert limit_match(_result(result="we should watch the 5-hour limit")) is None
        assert limit_match(None) is None

    def test_a_rejected_typed_window_wins_over_the_prose(self) -> None:
        # Structured data beats prose-grep: a seven-day window is the WEEKLY cause
        # however the agent's own final text happens to read.
        info = RateLimitInfo(status="rejected", rate_limit_type="seven_day")
        match = limit_match(_result(is_error=True, result="five hour limit reached"), info)
        assert match is not None
        assert match.cause is LimitCause.SUBSCRIPTION_WEEKLY

    def test_the_result_text_is_classified_when_no_window_was_rejected(self) -> None:
        match = limit_match(_result(is_error=True, result="Claude AI usage limit reached"), None)
        assert match is not None
