"""The deliberate negative control (teatree#1160 AC5/AC6).

AC5 asks for one scenario that intentionally violates a rule and asserts the
harness *reports the violation* — proving the harness catches what it is
supposed to catch. AC6 asks that the resulting red report identifies the
violated rule and the offending tool call.

The control is token-free and deterministic: it never shells ``claude -p``.
It builds the violating (and a compliant control) :class:`EvalRun` in process
and drives them through the public :func:`evaluate` report path.
"""

import json
from unittest.mock import patch

import pytest

from teatree.eval.negative_control import (
    NEGATIVE_CONTROL_SCENARIO,
    NegativeControlOutcome,
    build_compliant_run,
    build_violating_run,
    main,
    render_outcome,
    run_negative_control,
)
from teatree.eval.report import ScenarioResult, render_json, render_text


class TestNegativeControl:
    def test_harness_catches_the_planted_violation(self) -> None:
        outcome = run_negative_control()
        assert outcome.caught is True
        assert outcome.scenario_name == NEGATIVE_CONTROL_SCENARIO

    def test_outcome_names_the_violated_rule(self) -> None:
        outcome = run_negative_control()
        assert NEGATIVE_CONTROL_SCENARIO in outcome.violated_rule

    def test_outcome_names_the_offending_tool_call(self) -> None:
        outcome = run_negative_control()
        assert outcome.offending_tool_call is not None
        assert outcome.offending_tool_call.name == "Edit"

    def test_a_compliant_run_of_the_same_scenario_is_not_caught(self) -> None:
        # The control is anti-vacuous: it only fires on a genuine violation, so
        # a compliant run of the SAME scenario must NOT be reported as caught.
        # Without this, the control could be reporting red unconditionally.
        outcome = run_negative_control(run_factory=build_compliant_run)
        assert outcome.caught is False

    def test_violating_and_compliant_runs_differ_in_verdict(self) -> None:
        assert build_violating_run().tool_calls != build_compliant_run().tool_calls

    def test_raises_when_control_scenario_absent_from_catalog(self) -> None:
        with (
            patch("teatree.eval.negative_control.find_spec", return_value=None),
            pytest.raises(LookupError, match=NEGATIVE_CONTROL_SCENARIO),
        ):
            run_negative_control()


class TestNegativeControlReportContent:
    def test_text_report_identifies_violated_rule_and_offending_call(self) -> None:
        outcome = run_negative_control()
        text = render_text([outcome.result])
        assert text.startswith("FAIL")
        assert NEGATIVE_CONTROL_SCENARIO in text
        assert "Edit" in text

    def test_json_report_carries_the_offending_call_and_failed_matcher(self) -> None:
        outcome = run_negative_control()
        payload = json.loads(render_json([outcome.result]))
        [scenario] = payload["scenarios"]
        assert scenario["name"] == NEGATIVE_CONTROL_SCENARIO
        assert scenario["passed"] is False
        assert any(call["name"] == "Edit" for call in scenario["tool_calls"])
        assert any(not matcher["passed"] for matcher in scenario["matchers"])


class TestNegativeControlHonestWording:
    """A CAUGHT planted violation = the lane PASSED; the text must say so.

    The lane's own output previously embedded the inner scenario render, which
    reads ``FAIL worktree_first`` + ``1 failed`` — the planted violation being
    correctly detected, but framed as if the lane itself failed. A reader
    skimming it concludes the harness is broken when it just did its job.
    """

    def test_caught_outcome_text_states_pass_not_failure(self) -> None:
        text = render_outcome(run_negative_control(), as_json=False)
        assert "PASS" in text
        assert NEGATIVE_CONTROL_SCENARIO in text
        assert "Edit" in text
        # The misleading verdict framing must not appear for a CAUGHT lane:
        # neither a bare ``FAIL worktree_first`` line nor a ``N failed`` summary
        # that reads as the lane failing.
        assert f"FAIL {NEGATIVE_CONTROL_SCENARIO}" not in text
        assert "1 failed" not in text

    def test_missed_outcome_text_states_failure(self) -> None:
        missed = NegativeControlOutcome(
            scenario_name=NEGATIVE_CONTROL_SCENARIO,
            result=ScenarioResult(
                spec=run_negative_control().result.spec,
                run=build_compliant_run(),
                matcher_results=(),
                skipped=False,
            ),
            offending_tool_call=None,
        )
        text = render_outcome(missed, as_json=False)
        assert "FAIL" in text
        assert "MISSED" in text or "BROKEN" in text


class TestMainEntrypoint:
    def test_returns_zero_when_harness_catches_violation(self) -> None:
        assert main() == 0

    def test_returns_one_when_harness_misses_violation(self) -> None:
        missed = NegativeControlOutcome(
            scenario_name=NEGATIVE_CONTROL_SCENARIO,
            result=ScenarioResult(
                spec=run_negative_control().result.spec,
                run=build_compliant_run(),
                matcher_results=(),
                skipped=False,
            ),
            offending_tool_call=None,
        )
        with patch("teatree.eval.negative_control.run_negative_control", return_value=missed):
            assert main() == 1
