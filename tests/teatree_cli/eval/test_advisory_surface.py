"""An ``interactive``-surface scenario is graded and reported, but never gates a lane.

This is the property that let the ``claude-agent-sdk`` Dependabot quarantine go
(souliane/teatree#3855): a bundled-CLI rendering change reds only the advisory
interactive scenarios, so no gating verdict depends on the tool-call wire shape.
"""

from pathlib import Path

from teatree.cli.eval.all import _ai_lane_result
from teatree.eval.models import HEADLESS_SURFACE, INTERACTIVE_SURFACE, EvalRun, EvalSpec
from teatree.eval.report import ScenarioResult


def _result(name: str, *, surface: str, passed: bool) -> ScenarioResult:
    spec = EvalSpec(
        name=name,
        scenario="synthetic",
        agent_path="skills/rules/SKILL.md",
        prompt="do the thing",
        matchers=(),
        source_path=Path("synthetic.yaml"),
        surface=surface,
    )
    # is_error is the cheapest way to force a FAIL verdict with no matcher machinery.
    run = EvalRun(
        spec_name=name,
        tool_calls=(),
        text_blocks=(),
        terminal_reason="success",
        is_error=not passed,
        raw_stdout="",
        raw_stderr="",
    )
    return ScenarioResult(spec=spec, run=run, matcher_results=(), skipped=False)


def test_a_failing_interactive_scenario_does_not_fail_the_lane() -> None:
    lane = _ai_lane_result([_result("chip", surface=INTERACTIVE_SURFACE, passed=False)], backend="api", graded=True)
    assert lane.passed


def test_a_failing_interactive_scenario_is_still_reported() -> None:
    # Advisory is not invisible: the count must stay in the detail line, or a real
    # interactive regression becomes silent rather than merely non-blocking.
    lane = _ai_lane_result([_result("chip", surface=INTERACTIVE_SURFACE, passed=False)], backend="api", graded=True)
    assert "1 advisory failed" in lane.detail


def test_a_failing_headless_scenario_still_fails_the_lane() -> None:
    lane = _ai_lane_result([_result("slack", surface=HEADLESS_SURFACE, passed=False)], backend="api", graded=True)
    assert not lane.passed


def test_a_headless_failure_alongside_an_interactive_one_still_fails() -> None:
    results = [
        _result("chip", surface=INTERACTIVE_SURFACE, passed=False),
        _result("slack", surface=HEADLESS_SURFACE, passed=False),
    ]
    lane = _ai_lane_result(results, backend="api", graded=True)
    assert not lane.passed
    assert "1 failed" in lane.detail
