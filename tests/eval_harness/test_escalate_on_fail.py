"""Adaptive escalate-on-fail: confirm a trial-1 failure before failing the lane.

The PR-path eval runs each changed scenario at ``--trials 1`` for fast/cheap
feedback. A single LLM trial is noisy, so a lone red trial is not yet proof of a
real failure. ``escalate_on_fail`` re-runs ONLY the scenarios that failed trial 1
at higher trials and classifies each:

*   it passes on ANY escalation trial → ``flaky`` (NOT a hard red — the agent IS
    capable of the right behavior; trial 1 was an unlucky sample);
*   every escalation trial fails → ``confirmed`` (a real, non-flaky failure — the
    lane goes RED);
*   every escalation trial SKIPS → ``unresolved`` (the re-run never happened, so
    nothing cleared trial 1 — the lane goes RED too).

A scenario that passed or skipped trial 1 is never re-run.
"""

from pathlib import Path

import pytest

from teatree.cli.eval.escalate import EscalationOutcome, EscalationReport, escalate_failures, render_escalation_markdown
from teatree.cli.eval.single_trial import _render_escalation_text
from teatree.eval.models import HEADLESS_SURFACE, INTERACTIVE_SURFACE, EvalRun, EvalSpec, Matcher
from teatree.eval.report import ScenarioResult


def _spec(name: str, *, surface: str = HEADLESS_SURFACE) -> EvalSpec:
    return EvalSpec(
        name=name,
        surface=surface,
        scenario="text",
        agent_path="skills/code/SKILL.md",
        prompt="do",
        matchers=(Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="x"),),
        source_path=Path("/tmp/spec.yaml"),
    )


def _result(spec: EvalSpec, *, passed: bool, skipped: bool = False, cap_reason: str = "") -> ScenarioResult:
    reason = cap_reason or ("skipped: x" if skipped else ("success" if passed else "end_turn"))
    run = EvalRun(
        spec_name=spec.name,
        tool_calls=(),
        text_blocks=(),
        terminal_reason=reason,
        is_error=not passed and not skipped and not cap_reason,
        raw_stdout="",
        raw_stderr="",
    )
    return ScenarioResult(spec=spec, run=run, matcher_results=(), skipped=skipped)


class _ScriptedRunner:
    """Maps a scenario name to a queue of verdicts for its escalation trials.

    A ``bool`` entry is a clean pass/fail. A ``str`` entry is a cap reason
    (``max_turns``) for a trial whose matchers were satisfied by a partial
    trajectory the harness then truncated — the #2192 shape, which
    :attr:`ScenarioResult.passed` scores as a non-pass. A ``None`` entry is a
    SKIPPED trial — a re-run that never happened.
    """

    def __init__(self, scripts: dict[str, list[bool | str | None]]) -> None:
        self._iters = {name: iter(verdicts) for name, verdicts in scripts.items()}
        self.calls: dict[str, int] = {}

    def __call__(self, spec: EvalSpec) -> ScenarioResult:
        self.calls[spec.name] = self.calls.get(spec.name, 0) + 1
        verdict = next(self._iters[spec.name])
        if isinstance(verdict, str):
            return _result(spec, passed=True, cap_reason=verdict)
        return _result(spec, passed=bool(verdict), skipped=verdict is None)


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

    def test_flaky_when_a_clean_trial_passes_despite_a_cap_truncated_sibling(self) -> None:
        # The live CI shape recorded as CONFIRMED (2/3): two clean passes and one
        # cap-truncated trial. Passing ANY escalation trial is flaky, never a hard red.
        spec = _spec("capped_sibling")
        initial = [_result(spec, passed=False)]
        runner = _ScriptedRunner({"capped_sibling": [True, "max_turns", True]})
        report = escalate_failures(initial, runner, escalate_trials=3)
        outcome = report.outcomes[0]
        assert outcome.passes == 2
        assert outcome.classification == "flaky"
        assert outcome.cap_tainted
        assert not outcome.is_hard_red
        assert not report.hard_red

    def test_confirmed_when_every_escalation_trial_is_cap_truncated(self) -> None:
        spec = _spec("all_capped")
        initial = [_result(spec, passed=False)]
        runner = _ScriptedRunner({"all_capped": ["max_turns", "max_turns", "max_turns"]})
        report = escalate_failures(initial, runner, escalate_trials=3)
        outcome = report.outcomes[0]
        assert outcome.passes == 0
        assert outcome.classification == "confirmed"
        assert outcome.cap_tainted
        assert outcome.is_hard_red
        assert report.hard_red

    def test_an_all_skipped_escalation_does_not_clear_the_trial_one_failure(self) -> None:
        # The observed CI shape: trial 1 failed, every escalation trial SKIPPED, and the
        # lane went green on "FLAKY ... (0/3 escalation trials)". Not being able to re-run a
        # scenario is not evidence the agent is capable, so it must not clear the failure.
        spec = _spec("never_reran")
        initial = [_result(spec, passed=False)]
        runner = _ScriptedRunner({"never_reran": [None, None, None]})
        report = escalate_failures(initial, runner, escalate_trials=3)
        assert report.hard_red
        outcome = report.outcomes[0]
        assert outcome.classification == "unresolved"
        assert outcome.passes == 0

    def test_a_partially_skipped_escalation_that_really_failed_is_confirmed(self) -> None:
        # One trial actually ran and failed — the failure WAS re-proven, so this is a
        # confirmed red, not an unresolved one.
        spec = _spec("partly_skipped")
        initial = [_result(spec, passed=False)]
        runner = _ScriptedRunner({"partly_skipped": [None, False, None]})
        report = escalate_failures(initial, runner, escalate_trials=3)
        assert report.hard_red
        assert report.outcomes[0].classification == "confirmed"

    def test_a_partially_skipped_escalation_that_passed_once_is_flaky(self) -> None:
        spec = _spec("partly_skipped_pass")
        initial = [_result(spec, passed=False)]
        runner = _ScriptedRunner({"partly_skipped_pass": [None, True, None]})
        report = escalate_failures(initial, runner, escalate_trials=3)
        assert not report.hard_red
        assert report.outcomes[0].classification == "flaky"

    def test_an_all_skipped_advisory_escalation_is_reported_but_never_gates(self) -> None:
        # #3855 preserved: an interactive-surface scenario is classified exactly the same
        # way, it just does not red the lane.
        spec = _spec("advisory_never_reran", surface=INTERACTIVE_SURFACE)
        initial = [_result(spec, passed=False)]
        runner = _ScriptedRunner({"advisory_never_reran": [None, None]})
        report = escalate_failures(initial, runner, escalate_trials=2)
        assert not report.hard_red
        assert report.outcomes[0].classification == "unresolved"
        assert report.outcomes[0].advisory

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

    def test_unresolved_outcome_is_a_hard_red(self) -> None:
        outcome = EscalationOutcome(spec_name="s", trials=3, passes=0, classification="unresolved")
        assert outcome.is_hard_red

    def test_advisory_unresolved_outcome_is_not_a_hard_red(self) -> None:
        outcome = EscalationOutcome(spec_name="s", trials=3, passes=0, classification="unresolved", advisory=True)
        assert not outcome.is_hard_red


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

    def test_cap_taint_is_reported_but_never_gates(self) -> None:
        report = EscalationReport(
            outcomes=[
                EscalationOutcome(
                    spec_name="capped_sibling", trials=3, passes=2, classification="flaky", cap_tainted=True
                )
            ]
        )
        assert "cap-truncated" in render_escalation_markdown(report)
        assert "CAP-TRUNCATED" in _render_escalation_text(report)
        assert not report.outcomes[0].is_hard_red
        assert not report.hard_red

    def test_unresolved_is_counted_apart_from_flaky(self) -> None:
        # An unresolved row absorbed into the flaky count would read as a cleared
        # failure on the very dashboard the lane's verdict is read from.
        report = EscalationReport(
            outcomes=[
                EscalationOutcome(spec_name="flaky_one", trials=3, passes=1, classification="flaky"),
                EscalationOutcome(spec_name="never_reran", trials=3, passes=0, classification="unresolved"),
            ]
        )
        md = render_escalation_markdown(report)
        assert "0 confirmed, 1 flaky, 1 unresolved" in md
        assert "| never_reran | 0/3 | unresolved |" in md
