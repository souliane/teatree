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
    UnmeteredJudgeError,
    UnmeteredSdkRunError,
    assert_executed_when_required,
    assert_judge_was_metered,
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
        RunGuards.sdk_metered_total(
            backend=backend, executed=executed, total_cost_usd=sum(r.run.cost_usd for r in results)
        )

    @staticmethod
    def judge_metered(*, judge_requested: bool, results: list[ScenarioResult]) -> None:
        """Fail-loud when ``--judge`` graded zero of its judge-oracle scenarios.

        ``r.judge`` is set only for a scenario carrying a judge block (the eligible
        set); a non-skipped :class:`JudgeOutcome` means the judge actually graded.
        So a judge-oracle scenario that executed but whose judge skipped is the
        fake-green this turns RED — see :func:`assert_judge_was_metered`.
        """
        eligible = sum(1 for r in results if not r.skipped and r.judge is not None)
        calls = sum(1 for r in results if r.judge is not None and not r.judge.skipped)
        try:
            assert_judge_was_metered(judge_requested=judge_requested, judge_eligible=eligible, judge_calls=calls)
        except UnmeteredJudgeError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from None

    @staticmethod
    def sdk_metered_total(*, backend: str, executed: int, total_cost_usd: float) -> None:
        """Fail-loud the unmetered-$0 guard from a precomputed cost total.

        The single-run lane sums ``ScenarioResult.run.cost_usd``; the benchmark /
        matrix lane works in ``MatrixRow`` and sums ``cost_usd`` itself. Both share
        this one ``UnmeteredSdkRunError`` → ``typer.Exit`` translation so a
        fake-green $0 metered run turns RED identically wherever it is detected.
        """
        try:
            assert_sdk_run_was_metered(backend=backend, executed=executed, total_cost_usd=total_cost_usd)
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


#: Default relative cost-drift a scenario may rise before ``--gate-cost-regression`` fails it
#: (0.20 = +20% vs the baseline run's per-scenario cost). Tune with ``--cost-regression-tolerance``.
DEFAULT_COST_REGRESSION_TOLERANCE = 0.20


class RegressionGates:
    """Per-model baseline diffs that turn a regressed run RED at the CLI boundary.

    Both gates diff the just-persisted *record* against each model's current
    baseline run (excluding itself) and print the per-scenario drops; each
    returns ``True`` when the caller should exit non-zero. Shared by all three
    run shapes (single-trial, pass@k, matrix), so a cost blow-up fails loud in
    every lane, not only the single-trial one.
    """

    @staticmethod
    def scores(record: "EvalRunRecord", *, enabled: bool) -> bool:
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

    @staticmethod
    def costs(record: "EvalRunRecord", *, enabled: bool, tolerance: float) -> bool:
        """Diff *record*'s per-scenario cost against each model's baseline cost.

        Returns ``True`` (caller exits non-zero) when any scenario's cost rose by
        more than *tolerance* (relative drift) versus the baseline run. A scenario
        whose baseline cost is ``0.0`` (subscription/free baseline — no metered
        reference) has an undefined relative drift, so it is skipped, never flagged
        and never a divide-by-zero. When no model has a baseline at all, the gate
        reports "no cost baseline" and passes.
        """
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
            for entry in EvalRunRecord.cost_regression_diff(baseline=baseline_run, candidate=record, model=model):
                if entry.pct_increase is None:
                    continue
                if entry.pct_increase > tolerance:
                    any_regressed = True
                    typer.echo(
                        f"COST REGRESSED {entry.scenario_name} [{entry.model}]: "
                        f"${entry.baseline_cost_usd:.4f} -> ${entry.candidate_cost_usd:.4f} "
                        f"(+{entry.pct_increase:.0%}, tolerance {tolerance:.0%})"
                    )
        if not any_baseline:
            typer.echo("cost: no cost baseline recorded for these models — nothing to compare")
        return any_regressed


class CostBoundsGate:
    """The declarative absolute-ceiling cost gate, distinct from :class:`RegressionGates`.

    ``RegressionGates.costs`` diffs a run against a *mutable DB baseline run* and
    no-ops a zero-cost scenario. This gate checks the just-persisted run's
    per-scenario cost against the CHECKED-IN ``evals/cost_bounds.yaml`` ceilings:
    a scenario over ``bound_usd * (1 + margin)`` is RED, and a *configured*
    scenario the run recorded no cost for is RED too (fail-loud, never
    skip-as-pass). The ceiling survives a DB reset because it lives in the diff.
    """

    @staticmethod
    def check(record: "EvalRunRecord", *, enabled: bool) -> bool:
        """Check *record*'s per-scenario cost against the ceilings; print violations; True if any."""
        if not enabled:
            return False
        from teatree.eval.cost_bounds import check_cost_bounds, load_cost_bounds  # noqa: PLC0415

        config = load_cost_bounds()
        if not config.bounds:
            typer.echo("cost-bounds: no scenarios pinned in evals/cost_bounds.yaml — nothing to gate")
            return False
        result = check_cost_bounds(record.costs_by_scenario(), config)
        for violation in result.violations:
            typer.echo(violation.render())
        return result.failed


def finalize_single_run(  # noqa: PLR0913 — each kwarg threads one `eval run` flag through the persist+gate tail.
    results: list[ScenarioResult],
    *,
    specs: list[EvalSpec],
    max_turns: int | None,
    persist: bool,
    baseline: bool,
    gate_regressions: bool,
    gate_cost_regression: bool,
    cost_regression_tolerance: float,
    gate_cost_bounds: bool = False,
) -> bool:
    """Persist a single-trial run and run the score + cost baseline + cost-bounds gates.

    Returns ``True`` when the process should exit non-zero: any scenario
    failed, OR a score regression, OR a cost regression beyond tolerance, OR a
    declarative cost-bounds violation (over ceiling / configured-but-uncosted).
    With ``--no-persist`` the gates have no durable record to read and are
    skipped — only the scenario pass/fail decides the exit.
    """
    regressed = False
    cost_regressed = False
    cost_bounds_failed = False
    if persist:
        record = persist_single(results, specs=specs, max_turns=max_turns, baseline=baseline)
        regressed = RegressionGates.scores(record, enabled=gate_regressions)
        cost_regressed = RegressionGates.costs(
            record, enabled=gate_cost_regression, tolerance=cost_regression_tolerance
        )
        cost_bounds_failed = CostBoundsGate.check(record, enabled=gate_cost_bounds)
    return any(not r.passed for r in results) or regressed or cost_regressed or cost_bounds_failed


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
