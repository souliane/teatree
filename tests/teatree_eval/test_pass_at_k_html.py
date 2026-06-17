"""Per-trial transcript HTML report for a metered pass@k run.

The whole-suite summary renders one row per LANE; this report renders, per
scenario, EACH trial's PASS/FAIL plus the agent's transcript (reasoning + tool
calls) — the evidence a maintainer reads to diagnose a red lane. These tests pin
that the transcript actually appears in the output and that markup is escaped.
"""

from pathlib import Path

from teatree.eval.models import EvalRun, EvalSpec, EvalToolCall, Matcher
from teatree.eval.pass_at_k import PassAtKResult
from teatree.eval.pass_at_k_html import render_pass_at_k_html
from teatree.eval.report import MatcherResult, ScenarioResult


def _spec(name: str) -> EvalSpec:
    return EvalSpec(
        name=name,
        scenario=f"scenario {name}",
        agent_path="skills/code/SKILL.md",
        prompt="do",
        matchers=(),
        source_path=Path("/tmp/spec.yaml"),
    )


def _run(spec: EvalSpec, *, text: tuple[str, ...] = (), tool_calls: tuple[EvalToolCall, ...] = ()) -> EvalRun:
    return EvalRun(
        spec_name=spec.name,
        tool_calls=tool_calls,
        text_blocks=text,
        terminal_reason="success",
        is_error=False,
        raw_stdout="",
        raw_stderr="",
    )


def _scenario_result(
    spec: EvalSpec,
    *,
    passed: bool,
    text: tuple[str, ...] = (),
    tool_calls: tuple[EvalToolCall, ...] = (),
    matcher_results: tuple[MatcherResult, ...] = (),
) -> ScenarioResult:
    run = _run(spec, text=text, tool_calls=tool_calls)
    # An unsatisfied matcher makes ``passed`` False via report.ScenarioResult.passed,
    # so for a deliberate FAIL the caller supplies a failing matcher_result.
    return ScenarioResult(
        spec=spec, run=run, matcher_results=matcher_results, skipped=not passed and not matcher_results
    )


def _pass_at_k(
    spec: EvalSpec,
    *,
    passes: int,
    trials: int,
    trial_results: tuple[ScenarioResult, ...],
    skipped: bool = False,
) -> PassAtKResult:
    return PassAtKResult(
        spec_name=spec.name,
        trials=trials,
        passes=passes,
        require="any",
        skipped=skipped,
        trial_results=trial_results,
    )


class TestRendersPerTrialTranscript:
    def test_reasoning_text_block_appears_in_the_output(self) -> None:
        spec = _spec("verify_target_before_cherry_pick")
        trial = _scenario_result(spec, passed=True, text=("First I read the source branch to find the real SHA.",))
        html = render_pass_at_k_html([_pass_at_k(spec, passes=1, trials=1, trial_results=(trial,))])
        assert "First I read the source branch to find the real SHA." in html

    def test_tool_calls_appear_in_the_output(self) -> None:
        spec = _spec("plan_before_any_change_under_load")
        call = EvalToolCall(name="Bash", input={"command": "git log --oneline"}, turn=1)
        trial = _scenario_result(spec, passed=True, tool_calls=(call,))
        html = render_pass_at_k_html([_pass_at_k(spec, passes=1, trials=1, trial_results=(trial,))])
        assert "Bash" in html
        assert "git log --oneline" in html

    def test_each_of_three_trials_renders_its_own_block(self) -> None:
        spec = _spec("full_speed_fans_out_parallel_workers_not_serial")
        trials = tuple(_scenario_result(spec, passed=True, text=(f"trial {i} reasoning",)) for i in range(3))
        html = render_pass_at_k_html([_pass_at_k(spec, passes=3, trials=3, trial_results=trials)])
        for i in range(3):
            assert f"trial {i} reasoning" in html
        assert html.count("trial 1</h3>") + html.count("trial 1 ") >= 1

    def test_failing_matcher_message_is_shown_for_a_failed_trial(self) -> None:
        spec = _spec("team_mate_spawned_opus_never_sonnet")
        matcher = Matcher(kind="positive", tool="Task", arg_path="model", operator="contains", value="opus")
        failed = MatcherResult(matcher=matcher, passed=False, message="expected model=opus, got sonnet")
        trial = _scenario_result(spec, passed=False, matcher_results=(failed,))
        html = render_pass_at_k_html([_pass_at_k(spec, passes=0, trials=1, trial_results=(trial,))])
        assert "expected model=opus, got sonnet" in html
        assert "FAIL" in html

    def test_aggregate_pass_count_is_shown(self) -> None:
        spec = _spec("s")
        trials = (_scenario_result(spec, passed=True), _scenario_result(spec, passed=False, matcher_results=()))
        # 1 of 2 trials passed.
        result = PassAtKResult(spec_name="s", trials=2, passes=1, require="any", skipped=False, trial_results=trials)
        html = render_pass_at_k_html([result])
        assert "1/2 trials passed" in html


class TestSelfContainedAndEscaped:
    def test_values_are_html_escaped(self) -> None:
        spec = _spec("x")
        trial = _scenario_result(spec, passed=True, text=("<script>alert(1)</script>",))
        html = render_pass_at_k_html([_pass_at_k(spec, passes=1, trials=1, trial_results=(trial,))])
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html

    def test_is_self_contained_no_external_assets(self) -> None:
        spec = _spec("x")
        trial = _scenario_result(spec, passed=True, text=("ok",))
        html = render_pass_at_k_html([_pass_at_k(spec, passes=1, trials=1, trial_results=(trial,))])
        assert "<style>" in html
        assert "http://" not in html
        assert "https://" not in html

    def test_skipped_scenario_renders_without_trials(self) -> None:
        spec = _spec("skipped_one")
        result = _pass_at_k(spec, passes=0, trials=3, trial_results=(), skipped=True)
        html = render_pass_at_k_html([result])
        assert "skipped_one" in html
        assert "SKIP" in html
