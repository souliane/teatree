"""Adaptive escalate-on-fail: confirm a trial-1 failure before failing the lane.

The PR-path eval runs each changed scenario at ``--trials 1`` for fast/cheap
feedback. A single LLM trial is noisy, so a lone red trial is not yet proof of a
real failure. ``escalate_on_fail`` re-runs ONLY the scenarios that failed trial 1
at higher trials and classifies each:

*   it passes on ANY escalation trial → ``flaky`` (NOT a hard red — the agent IS
    capable of the right behavior; trial 1 was an unlucky sample);
*   every escalation trial fails → ``confirmed`` (a real, non-flaky failure — the
    lane goes RED).

A scenario that passed or skipped trial 1 is never re-run.
"""

from pathlib import Path

import pytest

from teatree.cli.eval.escalate import EscalationOutcome, EscalationReport, escalate_failures, render_escalation_markdown
from teatree.eval.models import EvalRun, EvalSpec, Matcher
from teatree.eval.report import ScenarioResult


def _spec(name: str) -> EvalSpec:
    return EvalSpec(
        name=name,
        scenario="text",
        agent_path="skills/code/SKILL.md",
        prompt="do",
        matchers=(Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="x"),),
        source_path=Path("/tmp/spec.yaml"),
    )


def _result(spec: EvalSpec, *, passed: bool, skipped: bool = False) -> ScenarioResult:
    reason = "skipped: x" if skipped else ("success" if passed else "end_turn")
    run = EvalRun(
        spec_name=spec.name,
        tool_calls=(),
        text_blocks=(),
        terminal_reason=reason,
        is_error=not passed and not skipped,
        raw_stdout="",
        raw_stderr="",
    )
    return ScenarioResult(spec=spec, run=run, matcher_results=(), skipped=skipped)


class _ScriptedRunner:
    """Maps a scenario name to a queue of pass/fail verdicts for its escalation trials."""

    def __init__(self, scripts: dict[str, list[bool]]) -> None:
        self._iters = {name: iter(verdicts) for name, verdicts in scripts.items()}
        self.calls: dict[str, int] = {}

    def __call__(self, spec: EvalSpec) -> ScenarioResult:
        self.calls[spec.name] = self.calls.get(spec.name, 0) + 1
        return _result(spec, passed=next(self._iters[spec.name]))


class TestEscalateFailures:
    def test_flaky_when_one_escalation_trial_passes(self) -> None:
        # trial 1 failed; the 3-trial escalation has one pass → flaky, NOT a hard red.
        spec = _spec("flaky_one")
        initial = [_result(spec, passed=False)]
        runner = _ScriptedRunner({"flaky_one": [False, True, False]})
        report = escalate_failures(initial, runner, escalate_trials=3)
        assert not report.hard_red
        outcome = report.outcomes[0]
        assert outcome.classification == "flaky"
        assert outcome.passes == 1
        assert outcome.trials == 3

    def test_confirmed_red_when_every_escalation_trial_fails(self) -> None:
        # trial 1 failed; every escalation trial fails too → confirmed, hard red.
        spec = _spec("solid_red")
        initial = [_result(spec, passed=False)]
        runner = _ScriptedRunner({"solid_red": [False, False, False]})
        report = escalate_failures(initial, runner, escalate_trials=3)
        assert report.hard_red
        outcome = report.outcomes[0]
        assert outcome.classification == "confirmed"
        assert outcome.passes == 0

    def test_passing_scenario_is_never_escalated(self) -> None:
        spec = _spec("green")
        initial = [_result(spec, passed=True)]
        runner = _ScriptedRunner({})
        report = escalate_failures(initial, runner, escalate_trials=3)
        assert not report.hard_red
        assert report.outcomes == []
        assert runner.calls == {}

    def test_skipped_scenario_is_never_escalated(self) -> None:
        spec = _spec("skip")
        initial = [_result(spec, passed=False, skipped=True)]
        runner = _ScriptedRunner({})
        report = escalate_failures(initial, runner, escalate_trials=3)
        assert not report.hard_red
        assert report.outcomes == []
        assert runner.calls == {}

    def test_only_failed_scenarios_are_escalated(self) -> None:
        green = _spec("green")
        red = _spec("red")
        initial = [_result(green, passed=True), _result(red, passed=False)]
        runner = _ScriptedRunner({"red": [True, False, False]})
        report = escalate_failures(initial, runner, escalate_trials=3)
        # Only the red scenario re-ran; the green one was never escalated.
        assert set(runner.calls) == {"red"}
        assert runner.calls["red"] == 3
        assert [o.spec_name for o in report.outcomes] == ["red"]
        assert report.outcomes[0].classification == "flaky"

    def test_mixed_flaky_and_confirmed_reds_the_lane_on_the_confirmed_one(self) -> None:
        flaky = _spec("flaky")
        confirmed = _spec("confirmed")
        initial = [_result(flaky, passed=False), _result(confirmed, passed=False)]
        runner = _ScriptedRunner({"flaky": [True, False, False], "confirmed": [False, False, False]})
        report = escalate_failures(initial, runner, escalate_trials=3)
        # One flaky (capable) + one confirmed (real) → the lane is RED on the confirmed.
        assert report.hard_red
        by_name = {o.spec_name: o for o in report.outcomes}
        assert by_name["flaky"].classification == "flaky"
        assert by_name["confirmed"].classification == "confirmed"

    def test_escalate_trials_must_be_at_least_two(self) -> None:
        spec = _spec("x")
        initial = [_result(spec, passed=False)]
        with pytest.raises(ValueError, match="escalate_trials"):
            escalate_failures(initial, _ScriptedRunner({"x": [False]}), escalate_trials=1)

    def test_no_failures_yields_a_green_report(self) -> None:
        spec = _spec("green")
        report = escalate_failures([_result(spec, passed=True)], _ScriptedRunner({}), escalate_trials=3)
        assert not report.hard_red
        assert report.outcomes == []


class TestEscalationOutcome:
    def test_flaky_outcome_is_not_a_hard_red(self) -> None:
        outcome = EscalationOutcome(spec_name="s", trials=3, passes=1, classification="flaky")
        assert not outcome.is_hard_red

    def test_confirmed_outcome_is_a_hard_red(self) -> None:
        outcome = EscalationOutcome(spec_name="s", trials=3, passes=0, classification="confirmed")
        assert outcome.is_hard_red


class TestRenderEscalationMarkdown:
    def test_empty_report_renders_nothing(self) -> None:
        assert render_escalation_markdown(EscalationReport(outcomes=[])) == ""

    def test_renders_a_classified_table(self) -> None:
        report = EscalationReport(
            outcomes=[
                EscalationOutcome(spec_name="flaky_one", trials=3, passes=1, classification="flaky"),
                EscalationOutcome(spec_name="solid_red", trials=3, passes=0, classification="confirmed"),
            ]
        )
        md = render_escalation_markdown(report)
        assert "1 confirmed, 1 flaky" in md
        assert "| flaky_one | 1/3 | flaky |" in md
        assert "| solid_red | 0/3 | confirmed |" in md
