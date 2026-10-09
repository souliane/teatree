"""Incomplete evals remain ungraded when written to the legacy verdict ledger."""

from pathlib import Path

from teatree.eval.models import EvalRun, EvalSpec
from teatree.eval.pass_at_k import PassAtKResult
from teatree.eval.persistence import _pass_at_k_verdict, _single_trial_verdict
from teatree.eval.report import ScenarioResult


def test_incomplete_single_and_multi_trial_use_ungraded_ledger_verdict() -> None:
    spec = EvalSpec(
        name="partial",
        scenario="partial",
        agent_path="skills/code/SKILL.md",
        prompt="do",
        matchers=(),
        source_path=Path("/tmp/spec.yaml"),
    )
    run = EvalRun(
        spec_name=spec.name,
        tool_calls=(),
        text_blocks=("evidence",),
        terminal_reason="success",
        is_error=False,
        raw_stdout="",
        raw_stderr="",
        coverage_incomplete=True,
    )
    trial = ScenarioResult(spec=spec, run=run, matcher_results=(), skipped=False)
    aggregate = PassAtKResult(
        spec_name=spec.name, trials=1, passes=1, require="any", skipped=False, trial_results=(trial,)
    )

    assert trial.verdict == "incomplete"
    assert aggregate.verdict == "incomplete"
    assert _single_trial_verdict(trial) == "error"
    assert _pass_at_k_verdict(aggregate) == "error"
