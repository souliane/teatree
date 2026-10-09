"""The Anthropic exhaustion classifier sorts each REAL signal into its distinct cause."""

from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from claude_agent_sdk.types import RateLimitType

from teatree.llm.anthropic_limits import (
    ALL_TOKENS_EXHAUSTED_SIGNATURE,
    REFUSAL_RESET_CEILING,
    REFUSAL_RESET_FLOOR,
    EgressBlockedError,
    LimitCause,
    LimitMatch,
    believable_refusal_reset,
    classify_limit,
    classify_rate_limit_type,
    parse_stated_reset,
    recoverable_exhaustion_cause,
    window_horizon,
)

_CYCLE_RESET = datetime(2026, 10, 1, tzinfo=UTC)


def _orca_body(message: str, *, code: str = "access_denied") -> str:
    """How ``str(ModelHTTPError)`` renders an OpenAI-compatible router's refusal."""
    return f"status_code: 403, model_name: m, body: {{'error': {{'message': '{message}', 'code': '{code}'}}}}"


class TestProviderBudgetClassification:
    @pytest.mark.parametrize(
        ("text", "reset"),
        [
            (_orca_body("token cycle spend limit reached, resets at 2026-10-01T00:00:00Z"), _CYCLE_RESET),
            (_orca_body("token quota is not enough", code="pre_consume_token_quota_failed"), None),
            (_orca_body("pre_consume_token_quota_failed"), None),
            (_orca_body("insufficient_user_quota"), None),
            (_orca_body("monthly budget reached for this member"), None),
        ],
    )
    def test_each_router_spend_stop_is_a_provider_budget(self, text: str, reset: datetime | None) -> None:
        match = classify_limit(text)
        assert match is not None
        assert match.cause is LimitCause.PROVIDER_BUDGET
        assert match.stated_reset == reset

    def test_the_same_body_without_its_spend_phrase_is_no_limit(self) -> None:
        assert classify_limit(_orca_body("access denied, resets at 2026-10-01T00:00:00Z")) is None

    def test_a_subscription_limit_never_carries_a_stated_reset(self) -> None:
        match = classify_limit("usage limit reached, resets at 2026-10-01T00:00:00Z")
        assert match is not None
        assert match.cause is LimitCause.SUBSCRIPTION_SESSION
        assert match.stated_reset is None

    def test_a_budget_stop_is_re_probed_after_six_hours_when_no_reset_is_stated(self) -> None:
        assert window_horizon(LimitCause.PROVIDER_BUDGET) == timedelta(hours=6)

    def test_the_reason_names_the_budget_and_never_the_anthropic_console(self) -> None:
        match = classify_limit(_orca_body("insufficient_user_quota"))
        assert match is not None
        reason = match.as_reason()
        assert reason.startswith("provider_budget: insufficient_user_quota — ")
        assert "console.anthropic.com" not in reason
        assert "subscription" not in reason.casefold()

    def test_a_budget_stop_is_not_auto_requeued_as_a_window_recovery(self) -> None:
        error = LimitMatch(phrase="insufficient_user_quota", cause=LimitCause.PROVIDER_BUDGET).as_reason()
        assert recoverable_exhaustion_cause(error) is None


class TestLeakBlockedClassification:
    """A content block — the router's guardrail or teatree's own egress scanner — is terminal, never a window."""

    _GUARDRAIL_BODY = (
        "status_code: 400, model_name: m, body: {'error': {'code': 'guardrail_blocked', "
        "'message': 'blocked by guardrail teatree-egress (rule secrets)', 'type': 'invalid_request_error'}}"
    )

    def test_a_router_guardrail_block_is_a_leak_block(self) -> None:
        match = classify_limit(self._GUARDRAIL_BODY)
        assert match is not None
        assert match.cause is LimitCause.LEAK_BLOCKED

    def test_an_egress_block_carries_its_own_marker(self) -> None:
        match = classify_limit(str(EgressBlockedError("secret-shaped token in the request body")))
        assert match is not None
        assert match.cause is LimitCause.LEAK_BLOCKED

    def test_a_leak_block_outranks_any_other_phrase_in_the_same_body(self) -> None:
        match = classify_limit(self._GUARDRAIL_BODY + " rate limit, monthly budget reached")
        assert match is not None
        assert match.cause is LimitCause.LEAK_BLOCKED

    def test_a_leak_block_has_no_window_and_is_never_auto_requeued(self) -> None:
        assert window_horizon(LimitCause.LEAK_BLOCKED) is None
        reason = LimitMatch(phrase="guardrail_blocked", cause=LimitCause.LEAK_BLOCKED).as_reason()
        assert reason.startswith("leak_blocked: guardrail_blocked — ")
        assert recoverable_exhaustion_cause(reason) is None

    def test_the_same_400_without_the_block_code_is_no_limit(self) -> None:
        assert classify_limit(self._GUARDRAIL_BODY.replace("guardrail_blocked", "invalid_request")) is None


class TestParseStatedReset:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("token cycle spend limit reached, resets at 2026-10-01T00:00:00Z", _CYCLE_RESET),
            ("resets at 2026-10-01T02:00:00+02:00", _CYCLE_RESET),
            ("resets at 2026-10-01 00:00:00 UTC", _CYCLE_RESET),
            ("Resets At 2026-10-01T00:00:00.000Z.", _CYCLE_RESET),
        ],
    )
    def test_an_iso_reset_is_read_as_an_aware_utc_instant(self, text: str, expected: datetime) -> None:
        stated = parse_stated_reset(text)
        assert stated == expected
        assert stated is not None
        assert stated.tzinfo is not None

    @pytest.mark.parametrize(
        "text",
        ["token cycle spend limit reached", "resets at midnight", "resets at 2026-13-45T00:00:00Z", ""],
    )
    def test_no_parseable_reset_is_none(self, text: str) -> None:
        assert parse_stated_reset(text) is None


class TestClassifyLimit:
    @pytest.mark.parametrize(
        ("text", "cause"),
        [
            # API-key credit exhaustion — the billed key at $0 (HTTP 400) /
            # metered usage-based-billing overage exhaustion (the REAL CLI strings).
            ("Your credit balance is too low to access the Anthropic API.", LimitCause.API_CREDIT),
            ("Credit balance too low", LimitCause.API_CREDIT),
            ("the request failed: you are out of credits", LimitCause.API_CREDIT),
            ("You're out of usage credits. Run /usage-credits to keep going.", LimitCause.API_CREDIT),
            ("rejected: out_of_credits — your org is out of usage", LimitCause.API_CREDIT),
            # Subscription weekly (7-day) window — incl. the per-model Opus/Sonnet
            # 7-day caps (the seven_day_opus / seven_day_sonnet prose labels).
            ("You've hit your weekly limit. It resets on Jun 14.", LimitCause.SUBSCRIPTION_WEEKLY),
            ("reached the 7-day limit", LimitCause.SUBSCRIPTION_WEEKLY),
            ("You've reached your Opus limit. It resets next week.", LimitCause.SUBSCRIPTION_WEEKLY),
            ("You've reached your Sonnet limit for this week.", LimitCause.SUBSCRIPTION_WEEKLY),
            # Subscription ~5h rolling session window.
            ("Claude usage limit reached for this session.", LimitCause.SUBSCRIPTION_SESSION),
            ("you hit the 5-hour limit", LimitCause.SUBSCRIPTION_SESSION),
            # Transient API rate / quota limit.
            ("HTTP 429: rate limit exceeded, retry later", LimitCause.RATE_LIMIT),
            ("Anthropic API quota exceeded; please back off and retry.", LimitCause.RATE_LIMIT),
            # Anthropic API error-body ``type`` codes: a 429 carries rate_limit_error,
            # a 529 carries overloaded_error — both transient (retry shortly).
            ('status_code: 429, body: {"error": {"type": "rate_limit_error"}}', LimitCause.RATE_LIMIT),
            ('status_code: 529, body: {"error": {"type": "overloaded_error"}}', LimitCause.RATE_LIMIT),
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

    def test_opus_and_sonnet_phrases_are_weekly_not_the_generic_session_limit(self) -> None:
        # The per-model 7-day caps render as "Opus limit" / "Sonnet limit" — they
        # are WEEKLY windows and must win over the generic session ``usage limit``.
        for text in ("You've hit your Opus limit.", "You've hit your Sonnet limit."):
            match = classify_limit(text)
            assert match is not None
            assert match.cause is LimitCause.SUBSCRIPTION_WEEKLY

    def test_overage_credit_strings_are_api_credit_never_subscription(self) -> None:
        # The REAL usage-based-billing $0 strings must reach the api-credit cause —
        # a $0 metered condition was previously caught by neither.
        for text in ("You're out of usage credits.", "error code: out_of_credits"):
            match = classify_limit(text)
            assert match is not None
            assert match.cause is LimitCause.API_CREDIT
            assert "subscription" not in match.as_reason().casefold()

    def test_quota_exceeded_is_a_transient_rate_limit_not_credit_or_subscription(self) -> None:
        # Re-added (finding 2) mapped to the transient rate/quota bucket — never
        # laundered into a credit or subscription cause.
        match = classify_limit("Anthropic API quota exceeded; back off.")
        assert match is not None
        assert match.cause is LimitCause.RATE_LIMIT
        assert "credit" not in match.as_reason().casefold()
        assert "subscription" not in match.as_reason().casefold()

    @pytest.mark.parametrize(
        ("status_code", "error_type"),
        [(429, "rate_limit_error"), (529, "overloaded_error")],
    )
    def test_the_api_error_code_literals_classify_without_any_prose(self, status_code: int, error_type: str) -> None:
        # A REAL Messages-API error body carries the error CODE, not the CLI's prose:
        # ``rate_limit_error`` (underscores) does not contain ``rate limit`` (space),
        # and a 529 body says only ``Overloaded``. Neither matched before, so a
        # provider-reported throttle on a direct-API lane classified as no limit at all.
        body = f"status_code: {status_code}, body: {{'error': {{'type': '{error_type}', 'message': 'Overloaded'}}}}"
        match = classify_limit(body)
        assert match is not None
        assert match.cause is LimitCause.RATE_LIMIT
        assert match.phrase == error_type, "the specific API code wins over the generic prose phrase"

    def test_the_api_error_codes_never_outrank_a_credit_or_subscription_signal(self) -> None:
        # The transient bucket stays LAST: a body naming both a throttle code and a
        # credit-empty balance is a credit condition, whose remediation is unrelated.
        match = classify_limit("{'type': 'rate_limit_error'} — your credit balance is too low")
        assert match is not None
        assert match.cause is LimitCause.API_CREDIT


class TestClassifyRateLimitType:
    """The SDK's TYPED ``RateLimitInfo.rate_limit_type`` window classifies unambiguously."""

    @pytest.mark.parametrize(
        ("window", "cause"),
        [
            ("five_hour", LimitCause.SUBSCRIPTION_SESSION),
            ("seven_day", LimitCause.SUBSCRIPTION_WEEKLY),
            ("seven_day_opus", LimitCause.SUBSCRIPTION_WEEKLY),
            ("seven_day_sonnet", LimitCause.SUBSCRIPTION_WEEKLY),
            ("overage", LimitCause.API_CREDIT),
        ],
    )
    def test_each_typed_window_maps_to_its_cause(self, window: RateLimitType, cause: LimitCause) -> None:
        match = classify_rate_limit_type(window)
        assert match is not None
        assert match.cause is cause
        assert match.phrase == window

    def test_opus_and_sonnet_seven_day_windows_are_weekly_via_the_typed_field(self) -> None:
        # Finding 3: the per-model 7-day windows that matched NO phrase signature
        # before now classify WEEKLY from the unambiguous typed field.
        for window in ("seven_day_opus", "seven_day_sonnet"):
            match = classify_rate_limit_type(cast("RateLimitType", window))
            assert match is not None
            assert match.cause is LimitCause.SUBSCRIPTION_WEEKLY

    def test_overage_window_is_the_credit_cause_with_the_console_remediation(self) -> None:
        match = classify_rate_limit_type("overage")
        assert match is not None
        assert match.cause is LimitCause.API_CREDIT
        assert "console.anthropic.com" in match.remediation
        assert "subscription" not in match.as_reason().casefold()

    def test_none_and_unknown_window_classify_as_no_limit(self) -> None:
        assert classify_rate_limit_type(None) is None
        # A future/unknown window value falls through to the phrase fallback.
        assert classify_rate_limit_type(cast("RateLimitType", "made_up_window")) is None


class TestRecoverableExhaustionCause:
    """The recorded-``error`` marker a window-recoverable exhaustion failure carries (#3407)."""

    @pytest.mark.parametrize(
        "cause",
        [LimitCause.SUBSCRIPTION_SESSION, LimitCause.SUBSCRIPTION_WEEKLY, LimitCause.RATE_LIMIT],
    )
    def test_recognises_the_reason_marker_a_recoverable_limit_records(self, cause: LimitCause) -> None:
        # The real string a limit-killed attempt stores (``LimitMatch.as_reason``).
        error = LimitMatch(phrase="5-hour limit", cause=cause).as_reason()
        assert recoverable_exhaustion_cause(error) is cause

    def test_api_credit_has_no_timed_recovery_and_is_never_auto_requeued(self) -> None:
        error = LimitMatch(phrase="out of credits", cause=LimitCause.API_CREDIT).as_reason()
        assert recoverable_exhaustion_cause(error) is None

    def test_a_non_limit_error_is_not_a_recoverable_exhaustion(self) -> None:
        assert recoverable_exhaustion_cause("AssertionError: expected 3 got 4") is None
        assert recoverable_exhaustion_cause("outage_death: connection refused") is None
        assert recoverable_exhaustion_cause("") is None

    def test_all_accounts_exhausted_is_window_recoverable_not_a_human_escalation(self) -> None:
        # A FAILED task with every configured account drained must auto-requeue, never escalate.
        error = f"all configured Anthropic oauth {ALL_TOKENS_EXHAUSTED_SIGNATURE} (accounts a, b)"
        assert recoverable_exhaustion_cause(error) is LimitCause.SUBSCRIPTION_WEEKLY


class TestBelievableRefusalReset:
    """The band a provider-REPORTED refusal reset must land inside (#4816).

    A refusal names no window teatree can verify, and nothing downstream bounds a park:
    ``effective_resets_at`` returns the reported instant verbatim and ``_future_park_instant``
    clamps only the lower end. So an unbelievable figure is refused here, where the one-hour
    horizon is still reachable, rather than persisted as a ``UsageWindowState``.
    """

    _NOW = 1_700_000_000.0

    def test_a_reset_inside_the_band_is_believed(self) -> None:
        assert believable_refusal_reset(self._NOW + 3600, now=self._NOW) == int(self._NOW + 3600)

    def test_both_edges_are_inclusive(self) -> None:
        for edge in (REFUSAL_RESET_FLOOR, REFUSAL_RESET_CEILING):
            at_edge = self._NOW + edge.total_seconds()
            assert believable_refusal_reset(at_edge, now=self._NOW) == int(at_edge)

    def test_a_key_lifetime_beyond_the_ceiling_is_refused(self) -> None:
        a_year = self._NOW + 365 * 24 * 3600
        assert believable_refusal_reset(a_year, now=self._NOW) is None

    def test_an_instant_under_the_floor_is_refused_rather_than_clamped_to_it(self) -> None:
        # The stale-``created``-stamp case: clamping it to the floor re-probes 12x an hour.
        assert believable_refusal_reset(self._NOW + 30, now=self._NOW) is None
        assert believable_refusal_reset(self._NOW - 86_400, now=self._NOW) is None
