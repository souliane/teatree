"""Anti-vacuous unit table for :func:`teatree.eval.triage.classify_red`.

Every triage class has a fixture that RED-fails if the classifier misroutes it —
the discriminator table (§3.5) is the anti-cheat boundary, so each class is
asserted against an input that would land in a DIFFERENT class under a wrong
precedence (an errored matcher-fail must be transport, not behavioral; a
cap-truncated matcher-fail must be infra_cap, not behavioral). The cap set and
throttle prefix are asserted to come from their canonical homes, not a copy.
"""

import pytest

from teatree.eval.api_errors import THROTTLE_TERMINAL_PREFIX, ThrottleKind, ThrottleSignal
from teatree.eval.models import CAP_TERMINAL_REASONS
from teatree.eval.throttle_retry import throttle_reason
from teatree.eval.triage import ScenarioTriage, TriageClass, classify_red


def _triage(
    *,
    verdict: str,
    is_error: bool = False,
    terminal_reason: str = "",
    matcher_failed: bool = False,
    judge_failed: bool = False,
) -> ScenarioTriage:
    return ScenarioTriage(
        verdict=verdict,
        is_error=is_error,
        terminal_reason=terminal_reason,
        matcher_failed=matcher_failed,
        judge_failed=judge_failed,
    )


class TestClassifyRedTable:
    def test_passing_scenario_is_not_red(self) -> None:
        assert classify_red(_triage(verdict="pass")) is None

    def test_behavioral_when_a_matcher_failed_cleanly(self) -> None:
        # A clean matcher diff (no error, no cap, no throttle) is the one class
        # the loop must FIX — misrouting it to an infra retry would ship the bug.
        result = _triage(verdict="fail", matcher_failed=True, terminal_reason="success")
        assert classify_red(result) is TriageClass.BEHAVIORAL

    def test_is_error_outranks_a_matcher_fail(self) -> None:
        # A transport error must be transport infra even when a matcher also failed
        # — a fix agent cannot repair a git-clone / SDK exit-1.
        result = _triage(verdict="fail", is_error=True, matcher_failed=True)
        assert classify_red(result) is TriageClass.INFRA_TRANSPORT

    def test_throttle_prefix_is_infra_throttle(self) -> None:
        result = _triage(verdict="fail", terminal_reason=f"{THROTTLE_TERMINAL_PREFIX} rate_limit (exhausted 5 retries)")
        assert classify_red(result) is TriageClass.INFRA_THROTTLE

    def test_real_throttle_reason_string_is_infra_throttle(self) -> None:
        # The classifier must catch the EXACT string throttle_reason() builds, not
        # a hand-written approximation — proves the shared prefix is not divergent.
        reason = throttle_reason(ThrottleSignal(kind=ThrottleKind.TRANSIENT, cause=None, wait_seconds=None), attempts=5)
        assert classify_red(_triage(verdict="fail", terminal_reason=reason)) is TriageClass.INFRA_THROTTLE

    @pytest.mark.parametrize("cap_reason", sorted(CAP_TERMINAL_REASONS))
    def test_every_cap_reason_is_infra_cap(self, cap_reason: str) -> None:
        # Each real cap reason (from the canonical set) with matchers ALSO failed
        # must classify as infra_cap — a cap-truncated trajectory is not trustworthy
        # behavioral signal, so it must not be routed to a code fix.
        result = _triage(verdict="fail", terminal_reason=cap_reason, matcher_failed=True)
        assert classify_red(result) is TriageClass.INFRA_CAP

    def test_judge_only_red_is_judge(self) -> None:
        result = _triage(verdict="fail", judge_failed=True, matcher_failed=False, terminal_reason="success")
        assert classify_red(result) is TriageClass.JUDGE

    def test_judge_and_matcher_both_failed_is_behavioral(self) -> None:
        # A judge red is only `judge` when EVERY matcher passed; a matcher diff
        # alongside it is a genuine behavioral fail.
        result = _triage(verdict="fail", judge_failed=True, matcher_failed=True, terminal_reason="success")
        assert classify_red(result) is TriageClass.BEHAVIORAL

    def test_skip_is_no_coverage(self) -> None:
        # A skip under --require-executed is a wiring bug that must never count as
        # green — even a skip whose run "looks" clean.
        assert classify_red(_triage(verdict="skip", terminal_reason="skipped: claude not on PATH")) is (
            TriageClass.NO_COVERAGE
        )


class TestScenarioTriageFromJson:
    def test_round_trips_a_json_scenario_record(self) -> None:
        scenario = {
            "name": "x",
            "verdict": "fail",
            "is_error": True,
            "terminal_reason": "success",
            "matcher_failed": False,
            "judge_failed": False,
        }
        assert classify_red(ScenarioTriage.from_json(scenario)) is TriageClass.INFRA_TRANSPORT

    def test_missing_keys_default_safely(self) -> None:
        # A partial record (only a verdict) must not raise — a fail with no other
        # signal falls through to the safe BEHAVIORAL default.
        assert classify_red(ScenarioTriage.from_json({"verdict": "fail"})) is TriageClass.BEHAVIORAL
