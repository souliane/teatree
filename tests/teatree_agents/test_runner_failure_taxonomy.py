"""The terminal-``ResultMessage`` failure taxonomy the headless driver folds through.

Two pure classifiers with one job each: decide whether a run was stopped by a
model-access limit (:func:`limit_match`), and otherwise describe a run that did
not complete cleanly so it is recorded rather than laundered into a completion
(:func:`error_result_reason`). Both are pure functions of the SDK message, so a
verdict is reproducible without a task, a harness, or a database.
"""

from datetime import timedelta

from claude_agent_sdk import ResultMessage
from claude_agent_sdk.types import RateLimitInfo

from teatree.agents.runner_failure_taxonomy import (
    RESULT_ERROR_PREFIX,
    TURN_CEILING_SUBTYPE,
    error_result_reason,
    limit_match,
)
from teatree.llm.anthropic_limits import (
    RECOVERABLE_EXHAUSTION_CAUSES,
    LimitCause,
    LimitMatch,
    recoverable_exhaustion_cause,
    window_horizon,
)


def _result(
    *,
    is_error: bool = False,
    subtype: str = "success",
    result: str = "",
    errors=None,
    api_error_status: int | None = None,
) -> ResultMessage:
    return ResultMessage(
        subtype=subtype,
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=1,
        session_id="s1",
        result=result or None,
        errors=errors,
        api_error_status=api_error_status,
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


class TestProviderAccessDenied:
    """A hard 401/403 is a provider REFUSAL, and no Anthropic prose names it (#4816).

    The measured shape: 307 tasks each recorded their own FAILED attempt against
    ``403 access_denied: token cycle spend limit reached`` because the phrase table is
    Anthropic vocabulary and the status was never read.
    """

    def test_a_403_is_a_provider_refusal(self) -> None:
        match = limit_match(
            _result(
                is_error=True,
                subtype="error_during_execution",
                result="access_denied: token cycle spend limit reached, resets at 2026-09-21T00:00:00Z",
                api_error_status=403,
            )
        )
        assert match is not None
        assert match.cause is LimitCause.PROVIDER_ACCESS_DENIED
        assert "403" in match.phrase

    def test_a_401_is_the_same_refusal(self) -> None:
        match = limit_match(_result(is_error=True, result="invalid api key", api_error_status=401))
        assert match is not None
        assert match.cause is LimitCause.PROVIDER_ACCESS_DENIED

    def test_a_429_still_classifies_as_the_transient_rate_limit(self) -> None:
        match = limit_match(_result(is_error=True, result="rate_limit_error", api_error_status=429))
        assert match is not None
        assert match.cause is LimitCause.RATE_LIMIT

    def test_a_self_imposed_turn_ceiling_is_never_a_provider_window(self) -> None:
        assert limit_match(_result(is_error=True, subtype=TURN_CEILING_SUBTYPE, api_error_status=403)) is None

    def test_a_healthy_result_carrying_the_status_is_not_a_limit(self) -> None:
        assert limit_match(_result(result="all good", api_error_status=403)) is None

    def test_the_window_auto_clears_rather_than_wedging_the_lane(self) -> None:
        # A horizonless cause (the API_CREDIT shape) would never re-arm, so a transient
        # 403 would park the lane forever. One re-probe an hour is the safe direction.
        assert window_horizon(LimitCause.PROVIDER_ACCESS_DENIED) == timedelta(hours=1)
        assert LimitCause.PROVIDER_ACCESS_DENIED in RECOVERABLE_EXHAUSTION_CAUSES

    def test_a_task_that_landed_failed_under_the_old_shape_is_recoverable(self) -> None:
        reason = LimitMatch(phrase="http 403", cause=LimitCause.PROVIDER_ACCESS_DENIED).as_reason()
        assert recoverable_exhaustion_cause(reason) is LimitCause.PROVIDER_ACCESS_DENIED

    def test_the_remediation_names_the_provider_console(self) -> None:
        remediation = LimitMatch(phrase="http 403", cause=LimitCause.PROVIDER_ACCESS_DENIED).remediation
        assert "console" in remediation.casefold()
