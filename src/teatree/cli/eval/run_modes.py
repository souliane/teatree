"""Persistence, judging, and regression-gating helpers for ``t3 eval run``.

The ``run`` command in :mod:`teatree.cli.eval.app` orchestrates three execution
shapes (single-trial, pass@k, model-matrix). This module holds the pieces those
shapes share that are not themselves the runner loop: the LLM-judge grader
closure, the three persistence entry points (single / pass@k / matrix), and the
baseline regression gate. Keeping them here keeps the command module focused on
the typer surface and the runner loop.
"""

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from teatree.eval.judge import ClaudeJudge, JudgeBudget
from teatree.eval.matrix import MatrixRow
from teatree.eval.models import EvalRun, EvalSpec
from teatree.eval.pass_at_k import PassAtKResult
from teatree.eval.report import JudgeGrader, JudgeOutcome, ScenarioResult
from teatree.eval.skip_guard import (
    AllSkippedError,
    UnmeteredSdkRunError,
    assert_executed_when_required,
    assert_sdk_run_was_metered,
)

if TYPE_CHECKING:
    from teatree.core.models import EvalRunRecord


class RunGuards:
    """Translate the no-coverage :mod:`teatree.eval.skip_guard` assertions into a CLI exit.

    Both guards turn a vacuous-green run RED at the ``t3 eval run`` boundary: an
    all-skipped required run, and an sdk run that executed scenarios but metered $0.
    """

    @staticmethod
    def executed(*, executed: int, collected: int, required: bool) -> None:
        try:
            assert_executed_when_required(collected=collected, executed=executed, required=required)
        except AllSkippedError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from None

    @staticmethod
    def sdk_metered(*, backend: str, executed: int, results: list[ScenarioResult]) -> None:
        try:
            assert_sdk_run_was_metered(
                backend=backend,
                executed=executed,
                total_cost_usd=sum(r.run.cost_usd for r in results),
            )
        except UnmeteredSdkRunError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from None


def run_model_label(specs: list[EvalSpec]) -> str:
    models = sorted({spec.model for spec in specs})
    return ",".join(models)


def with_model(spec: EvalSpec, model: str) -> EvalSpec:
    return dataclasses.replace(spec, model=model)


def make_grader(*, enabled: bool, judge_budget: int) -> JudgeGrader | None:
    """Return an LLM-judge grader closure when ``--judge`` is set, else ``None``."""
    if not enabled:
        return None
    claude_judge = ClaudeJudge(budget=JudgeBudget(max_calls=judge_budget))

    def _grade(spec: EvalSpec, run: EvalRun) -> JudgeOutcome:
        verdict = claude_judge.grade(spec, run)
        return JudgeOutcome(passed=verdict.passed, skipped=verdict.skipped, rationale=verdict.rationale)

    return _grade


def persist_single(
    results: list[ScenarioResult],
    *,
    specs: list[EvalSpec],
    max_turns: int | None,
    baseline: bool,
) -> "EvalRunRecord":
    from teatree.eval.persistence import persist_run  # noqa: PLC0415

    record = persist_run(results, model=run_model_label(specs), max_turns_override=max_turns)
    if baseline:
        record.mark_baseline()
    return record


def persist_pass_at_k_run(
    results: list[PassAtKResult],
    *,
    model: str,
    max_turns: int | None,
    baseline: bool,
) -> "EvalRunRecord":
    from teatree.eval.persistence import persist_pass_at_k  # noqa: PLC0415

    record = persist_pass_at_k(results, model=model, max_turns_override=max_turns)
    if baseline:
        record.mark_baseline()
    return record


def persist_matrix_run(
    rows: list[MatrixRow],
    *,
    models: list[str],
    max_turns: int | None,
    baseline: bool,
) -> "EvalRunRecord":
    from teatree.eval.persistence import persist_matrix  # noqa: PLC0415

    record = persist_matrix(rows, models=models, max_turns_override=max_turns)
    if baseline:
        record.mark_baseline()
    return record


def gate_run_regressions(record: "EvalRunRecord", *, enabled: bool) -> bool:
    """Diff *record* against each model's baseline; print drops; True if any regressed."""
    if not enabled:
        return False
    from teatree.core.models import EvalRunRecord  # noqa: PLC0415

    any_regressed = False
    any_baseline = False
    for model in record.models:
        baseline_run = EvalRunRecord.objects.for_model(model).baselines().exclude(pk=record.pk).first()
        if baseline_run is None:
            continue
        any_baseline = True
        for entry in EvalRunRecord.regression_diff(baseline=baseline_run, candidate=record, model=model):
            if entry.regressed:
                any_regressed = True
                typer.echo(
                    f"REGRESSED {entry.scenario_name} [{entry.model}]: "
                    f"{entry.baseline_pass_rate:.2f} -> {entry.candidate_pass_rate:.2f}"
                )
            elif entry.improved:
                typer.echo(
                    f"IMPROVED {entry.scenario_name} [{entry.model}]: "
                    f"{entry.baseline_pass_rate:.2f} -> {entry.candidate_pass_rate:.2f}"
                )
    if not any_baseline:
        typer.echo("baseline: no baseline recorded for these models — nothing to compare")
    return any_regressed


def build_subscription_manifest(specs: list[EvalSpec], target_dir: Path) -> list[dict[str, str]]:
    return [
        {
            "scenario": spec.name,
            "agent_path": spec.agent_path,
            "model": spec.model,
            "prompt": spec.prompt,
            "transcript_path": str(target_dir / f"{spec.name}.jsonl"),
        }
        for spec in specs
    ]


def render_subscription_text(manifest: list[dict[str, str]]) -> str:
    blocks = [
        (
            f"scenario: {entry['scenario']}  (model {entry['model']})\n"
            f"  agent:        {entry['agent_path']}\n"
            f"  capture to:   {entry['transcript_path']}  (via `t3 eval capture-subagent {entry['scenario']}`)\n"
            f"  prompt:       {entry['prompt']}\n"
        )
        for entry in manifest
    ]
    return "\n".join(blocks)
