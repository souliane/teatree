"""The Anthropic exhaustion classifier sorts each REAL signal into its distinct cause."""

import pytest

from teatree.llm.anthropic_limits import LimitCause, LimitMatch, classify_limit


class TestClassifyLimit:
    @pytest.mark.parametrize(
        ("text", "cause"),
        [
            # API-key credit exhaustion — the billed key at $0 (HTTP 400).
            ("Your credit balance is too low to access the Anthropic API.", LimitCause.API_CREDIT),
            ("Credit balance too low", LimitCause.API_CREDIT),
            ("the request failed: you are out of credits", LimitCause.API_CREDIT),
            # Subscription weekly (7-day) window.
            ("You've hit your weekly limit. It resets on Jun 14.", LimitCause.SUBSCRIPTION_WEEKLY),
            ("reached the 7-day limit", LimitCause.SUBSCRIPTION_WEEKLY),
            # Subscription ~5h rolling session window.
            ("Claude usage limit reached for this session.", LimitCause.SUBSCRIPTION_SESSION),
            ("you hit the 5-hour limit", LimitCause.SUBSCRIPTION_SESSION),
            # Transient API rate limit.
            ("HTTP 429: rate limit exceeded, retry later", LimitCause.RATE_LIMIT),
        ],
    )
    def test_classifies_each_real_signal_distinctly(self, text: str, cause: LimitCause) -> None:
        match = classify_limit(text)
        assert match is not None
        assert match.cause is cause

    def test_credit_precedes_subscription_and_remediation_names_the_console(self) -> None:
        match = classify_limit("Your credit balance is too low.")
        assert match is not None
        assert match.cause is LimitCause.API_CREDIT
        assert "console.anthropic.com" in match.remediation
        assert "subscription" not in match.as_reason().casefold()

    def test_weekly_is_not_mislabeled_as_the_generic_session_usage_limit(self) -> None:
        # A 7-day message must classify weekly, never the 5-hour session cause.
        match = classify_limit("You've reached your weekly limit.")
        assert match is not None
        assert match.cause is LimitCause.SUBSCRIPTION_WEEKLY

    def test_session_and_weekly_remediations_read_distinctly(self) -> None:
        session = classify_limit("usage limit reached")
        weekly = classify_limit("weekly limit reached")
        assert session is not None
        assert weekly is not None
        assert "same day" in session.remediation
        assert "weekly reset" in weekly.remediation
        assert session.remediation != weekly.remediation

    def test_unrelated_text_classifies_as_no_limit(self) -> None:
        assert classify_limit("some other failure about a socket") is None
        assert classify_limit("") is None

    def test_as_reason_leads_with_the_machine_cause_marker(self) -> None:
        match = LimitMatch(phrase="weekly limit", cause=LimitCause.SUBSCRIPTION_WEEKLY)
        assert match.as_reason().startswith("subscription_weekly: weekly limit — ")
