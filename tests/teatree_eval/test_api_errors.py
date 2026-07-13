"""Throttle classification beside the terminal-error classifier for the eval runner.

The metered ``api`` lane drives many parallel scenarios through ONE shared OAuth
token, so a burst turns per-token rate limits into false reds. Layer 1 grades a
raw SDK error message into a retry disposition — TRANSIENT (fast backoff),
SUSTAINED (window wait), or ``None`` (never a throttle: a genuine cap, credit
exhaustion, a 7-day weekly cap, a success mislabel, or a real crash).
"""

from teatree.eval.api_errors import ThrottleKind, classify_transient_throttle
from teatree.llm.anthropic_limits import LimitCause, window_horizon


class TestClassifyTransientThrottle:
    def test_rate_limit_is_transient(self) -> None:
        signal = classify_transient_throttle("Claude Code returned an error result: rate limit exceeded (429)")
        assert signal is not None
        assert signal.kind is ThrottleKind.TRANSIENT
        assert signal.cause is LimitCause.RATE_LIMIT

    def test_overloaded_is_transient(self) -> None:
        signal = classify_transient_throttle("Overloaded")
        assert signal is not None
        assert signal.kind is ThrottleKind.TRANSIENT

    def test_dropped_stream_is_transient(self) -> None:
        # A transport drop under load (a reset connection) carries no limit phrase
        # but is a transient infra signature, so it is ridden out.
        signal = classify_transient_throttle("peer closed connection unexpectedly")
        assert signal is not None
        assert signal.kind is ThrottleKind.TRANSIENT
        assert signal.cause is None

    def test_opaque_error_result_is_not_a_throttle(self) -> None:
        # An SDK error result carrying NO recognizable throttle signature is a
        # genuine crash that must re-raise — never laundered into a retry. This is
        # the "preserve the genuine-crash red" contract the runner relies on.
        assert classify_transient_throttle("Claude Code returned an error result: error_during_execution") is None

    def test_session_limit_is_sustained_with_window_wait(self) -> None:
        signal = classify_transient_throttle("session limit reached")
        assert signal is not None
        assert signal.kind is ThrottleKind.SUSTAINED
        assert signal.cause is LimitCause.SUBSCRIPTION_SESSION
        horizon = window_horizon(LimitCause.SUBSCRIPTION_SESSION)
        assert horizon is not None
        assert signal.wait_seconds == horizon.total_seconds()

    def test_api_credit_is_never_retried(self) -> None:
        # A $0 metered key has no time-based recovery — fail loud, never retry.
        assert classify_transient_throttle("credit balance is too low") is None

    def test_weekly_limit_is_never_retried(self) -> None:
        # A 7-day wait is never right inside a single run — surface loud, don't wait.
        assert classify_transient_throttle("weekly limit reached") is None

    def test_max_turns_cap_is_never_a_throttle(self) -> None:
        # The anti-cheat boundary: a genuine behavioral cap must not be laundered
        # into a retry that hides the real fail behind a backoff.
        assert classify_transient_throttle("Reached maximum number of turns (3)") is None

    def test_budget_cap_is_never_a_throttle(self) -> None:
        assert classify_transient_throttle("Reached maximum budget ($0.1)") is None

    def test_success_mislabel_is_not_a_throttle(self) -> None:
        assert classify_transient_throttle("Claude Code returned an error result: success") is None

    def test_genuine_crash_is_not_a_throttle(self) -> None:
        # A real bug carries no SDK error-result marker and no limit phrase — it
        # must re-raise as a genuine red, never be swallowed into a retry.
        assert classify_transient_throttle("TypeError: 'NoneType' object is not subscriptable") is None
        assert classify_transient_throttle("KeyError: 'foo'") is None
