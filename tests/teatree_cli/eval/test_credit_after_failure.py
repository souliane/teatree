"""A dead metered key after a measured failure stays red in the matrix and pass@k lanes; otherwise it parks."""

from pathlib import Path

import pytest
import typer

from teatree.cli.eval.metered_trials import exit_on_usage_limit_or_prior_red, run_pass_at_k_results
from teatree.cli.eval.multi_trial import MatrixUsageLimitError, TrialPolicy, collect_matrix_rows
from teatree.cli.eval.run_modes import park_credit_exhaustion
from teatree.eval.models import EvalRun, EvalSpec, Matcher
from teatree.eval.report import ScenarioResult, evaluate
from teatree.eval.skip_guard import MEASURED_NOTHING_EXIT_CODE
from teatree.llm.anthropic_limits import CreditExhaustedError

_MODEL = "claude-opus-4-8"


def _spec(name: str) -> EvalSpec:
    return EvalSpec(
        name=name,
        scenario=f"scenario {name}",
        agent_path="skills/code/SKILL.md",
        prompt="do",
        matchers=(
            Matcher(kind="positive", tool="Bash", arg_path="command", operator="contains", value="git worktree"),
        ),
        source_path=Path("/tmp/spec.yaml"),
        model=_MODEL,
    )


def _failing_run(spec: EvalSpec) -> EvalRun:
    return EvalRun(
        spec_name=spec.name,
        tool_calls=(),
        text_blocks=(),
        terminal_reason="success",
        is_error=False,
        raw_stdout="",
        raw_stderr="",
        cost_usd=0.0,
    )


def _dead_key(_spec: EvalSpec) -> EvalRun:
    message = "credits exhausted"
    raise CreditExhaustedError(message)


class _Script:
    """Runs each call in order: the n-th call to ``run`` uses the n-th behaviour."""

    def __init__(self, *behaviours) -> None:
        self._behaviours = list(behaviours)

    def run(self, spec: EvalSpec) -> EvalRun:
        return self._behaviours.pop(0)(spec)


def _trial(script: _Script):
    def trial(spec: EvalSpec) -> ScenarioResult:
        return evaluate(spec, script.run(spec))

    return trial


class TestPassAtKLane:
    def test_a_dead_key_after_a_failed_scenario_is_red_not_parked(self) -> None:
        specs = [_spec("alpha"), _spec("beta")]
        trial = _trial(_Script(_failing_run, _dead_key))

        with pytest.raises(typer.Exit) as exit_info, park_credit_exhaustion():
            run_pass_at_k_results(specs, trial, trials=1, require="all", summary_md=None)

        assert exit_info.value.exit_code == 1

    def test_a_dead_key_after_a_failed_trial_of_the_same_scenario_is_red_not_parked(self) -> None:
        trial = _trial(_Script(_failing_run, _dead_key))

        with pytest.raises(typer.Exit) as exit_info, park_credit_exhaustion():
            run_pass_at_k_results([_spec("alpha")], trial, trials=2, require="all", summary_md=None)

        assert exit_info.value.exit_code == 1

    def test_a_dead_key_with_nothing_measured_before_it_parks(self) -> None:
        trial = _trial(_Script(_dead_key))

        with pytest.raises(typer.Exit) as exit_info, park_credit_exhaustion():
            run_pass_at_k_results([_spec("alpha")], trial, trials=1, require="all", summary_md=None)

        assert exit_info.value.exit_code == MEASURED_NOTHING_EXIT_CODE


class TestMatrixLane:
    def test_a_dead_key_after_a_failed_cell_is_red_not_parked(self) -> None:
        specs = [_spec("alpha"), _spec("beta")]

        with pytest.raises(MatrixUsageLimitError) as raised:
            collect_matrix_rows(specs, [_MODEL], runner=_Script(_failing_run, _dead_key), policy=TrialPolicy(trials=1))

        assert raised.value.failed_before_limit(specs)
        with pytest.raises(typer.Exit) as exit_info:
            exit_on_usage_limit_or_prior_red(failed_before_limit=raised.value.failed_before_limit(specs))
        assert exit_info.value.exit_code == 1

    def test_a_dead_key_with_no_failed_cell_before_it_propagates_to_park(self) -> None:
        with pytest.raises(typer.Exit) as exit_info, park_credit_exhaustion():
            collect_matrix_rows([_spec("alpha")], [_MODEL], runner=_Script(_dead_key), policy=TrialPolicy(trials=1))

        assert exit_info.value.exit_code == MEASURED_NOTHING_EXIT_CODE
