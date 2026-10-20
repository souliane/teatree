from pathlib import Path

import pytest
import typer

from teatree.cli.eval.metered_trials import (
    UsageLimitAfterFailureError,
    exit_on_usage_limit_or_prior_red,
    run_pass_at_k_results,
    run_trials_with_limit_evidence,
)
from teatree.eval.api_errors import UsageLimitReachedError
from teatree.eval.models import EvalRun, EvalSpec
from teatree.eval.report import ScenarioResult


def _spec(name: str = "scenario") -> EvalSpec:
    return EvalSpec(
        name=name,
        scenario=name,
        agent_path="skills/code/SKILL.md",
        prompt="run",
        matchers=(),
        source_path=Path("scenario.yaml"),
    )


def _red(spec: EvalSpec) -> ScenarioResult:
    run = EvalRun(
        spec_name=spec.name,
        tool_calls=(),
        text_blocks=("failed",),
        terminal_reason="end_turn",
        is_error=True,
        raw_stdout="",
        raw_stderr="",
    )
    return ScenarioResult(spec=spec, run=run, matcher_results=(), skipped=False)


def test_all_of_k_preserves_a_measured_failure_before_the_limit() -> None:
    calls = 0

    def trial(spec: EvalSpec) -> ScenarioResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _red(spec)
        raise UsageLimitReachedError

    with pytest.raises(UsageLimitAfterFailureError):
        run_trials_with_limit_evidence(_spec(), trial, k=2, require="all")
    assert calls == 2


@pytest.mark.parametrize(("failed", "code", "status"), [(False, 75, "PARKED"), (True, 1, "RED")])
def test_limit_exit_records_the_matching_verdict(tmp_path: Path, *, failed: bool, code: int, status: str) -> None:
    summary = tmp_path / "summary.md"
    with pytest.raises(typer.Exit) as exc:
        exit_on_usage_limit_or_prior_red(summary_md=summary, failed_before_limit=failed)
    assert exc.value.exit_code == code
    assert status in summary.read_text()


def test_result_loop_stops_after_a_measured_red_and_a_limit(tmp_path: Path) -> None:
    attempted: list[str] = []

    def trial(spec: EvalSpec) -> ScenarioResult:
        attempted.append(spec.name)
        if spec.name == "red":
            return _red(spec)
        raise UsageLimitReachedError

    summary = tmp_path / "summary.md"
    with pytest.raises(typer.Exit) as exc:
        run_pass_at_k_results(
            [_spec("red"), _spec("limited"), _spec("later")],
            trial,
            trials=1,
            require="any",
            summary_md=summary,
        )
    assert exc.value.exit_code == 1
    assert attempted == ["red", "limited"]
    assert "RED" in summary.read_text()
