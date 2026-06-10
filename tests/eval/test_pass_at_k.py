"""pass@k aggregates multiple trials of a scenario."""

from pathlib import Path

import pytest

from teatree.eval.models import EvalRun, EvalSpec, Matcher
from teatree.eval.pass_at_k import run_pass_at_k
from teatree.eval.report import ScenarioResult


def _spec() -> EvalSpec:
    return EvalSpec(
        name="flaky",
        scenario="text",
        agent_path="skills/code/SKILL.md",
        prompt="do",
        matchers=(Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="x"),),
        source_path=Path("/tmp/spec.yaml"),
    )


def _result(spec: EvalSpec, *, passed: bool, skipped: bool = False, cost_usd: float = 0.0) -> ScenarioResult:
    run = EvalRun(
        spec_name=spec.name,
        tool_calls=(),
        text_blocks=(),
        terminal_reason="skipped: x" if skipped else "success",
        is_error=not passed and not skipped,
        raw_stdout="",
        raw_stderr="",
        cost_usd=cost_usd,
    )
    return ScenarioResult(spec=spec, run=run, matcher_results=(), skipped=skipped)


def _sequence_runner(spec: EvalSpec, verdicts: list[bool]):
    it = iter(verdicts)

    def _run(_spec: EvalSpec) -> ScenarioResult:
        return _result(spec, passed=next(it))

    return _run


class TestRunPassAtK:
    def test_any_passes_when_one_trial_passes(self) -> None:
        spec = _spec()
        result = run_pass_at_k(spec, _sequence_runner(spec, [False, True, False]), k=3, require="any")
        assert result.ok
        assert result.passes == 1
        assert result.trials == 3

    def test_all_fails_when_one_trial_fails(self) -> None:
        spec = _spec()
        result = run_pass_at_k(spec, _sequence_runner(spec, [True, False, True]), k=3, require="all")
        assert not result.ok
        assert result.passes == 2

    def test_all_passes_when_every_trial_passes(self) -> None:
        spec = _spec()
        result = run_pass_at_k(spec, _sequence_runner(spec, [True, True]), k=2, require="all")
        assert result.ok
        assert result.passes == result.trials

    def test_any_fails_when_no_trial_passes(self) -> None:
        spec = _spec()
        result = run_pass_at_k(spec, _sequence_runner(spec, [False, False]), k=2, require="any")
        assert not result.ok
        assert result.passes == 0

    def test_all_trials_skipped_marks_skipped_and_ok(self) -> None:
        spec = _spec()
        result = run_pass_at_k(spec, lambda s: _result(s, passed=False, skipped=True), k=3)
        assert result.skipped
        assert result.ok

    def test_rejects_bad_k(self) -> None:
        spec = _spec()
        with pytest.raises(ValueError, match="k must be"):
            run_pass_at_k(spec, lambda s: _result(s, passed=True), k=0)

    def test_rejects_bad_require(self) -> None:
        spec = _spec()
        with pytest.raises(ValueError, match="require must be"):
            run_pass_at_k(spec, lambda s: _result(s, passed=True), k=1, require="most")

    def test_sums_cost_across_every_trial(self) -> None:
        spec = _spec()
        result = run_pass_at_k(spec, lambda s: _result(s, passed=True, cost_usd=0.25), k=3)
        assert result.cost_usd == pytest.approx(0.75)
