from pathlib import Path

import pytest
import typer

from teatree.cli.eval.metered_trials import (
    UsageLimitAfterFailureError,
    park_on_usage_limit,
    run_trials_with_limit_evidence,
)
from teatree.eval.api_errors import UsageLimitReachedError
from teatree.eval.models import EvalRun, EvalSpec
from teatree.eval.report import ScenarioResult


def _failed_result(spec: EvalSpec) -> ScenarioResult:
    run = EvalRun(
        spec_name=spec.name,
        tool_calls=(),
        text_blocks=(),
        terminal_reason="success",
        is_error=True,
        raw_stdout="",
        raw_stderr="",
    )
    return ScenarioResult(spec=spec, run=run, matcher_results=(), skipped=False)


@pytest.mark.parametrize("require", ["any", "all"])
def test_limit_after_one_failed_trial_only_confirms_red_for_all(require: str) -> None:
    spec = EvalSpec(
        name="alpha",
        scenario="synthetic",
        agent_path="skills/rules/SKILL.md",
        prompt="go",
        matchers=(),
        source_path=Path("synthetic.yaml"),
    )
    calls = 0

    def trial(candidate: EvalSpec) -> ScenarioResult:
        nonlocal calls
        calls += 1
        if calls == 2:
            message = "limit"
            raise UsageLimitReachedError(message)
        return _failed_result(candidate)

    with pytest.raises(UsageLimitReachedError) as exc:
        run_trials_with_limit_evidence(spec, trial, k=2, require=require)
    assert isinstance(exc.value, UsageLimitAfterFailureError) is (require == "all")


def test_usage_limit_writes_a_parked_artifact(tmp_path: Path) -> None:
    summary = tmp_path / "summary.md"
    with pytest.raises(typer.Exit) as exc:
        park_on_usage_limit(summary_md=summary)
    assert exc.value.exit_code == 75
    assert "PARKED" in summary.read_text()


def test_prior_failure_keeps_the_red_verdict_and_dashboard(tmp_path: Path) -> None:
    dashboard = tmp_path / "dashboard.html"
    with pytest.raises(typer.Exit) as exc:
        park_on_usage_limit(html_out=dashboard, failed_before_limit=True)
    assert exc.value.exit_code == 1
    assert "failed before the usage limit" in dashboard.read_text()
