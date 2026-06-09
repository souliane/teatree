"""``t3 eval run`` multi-trial (pass@k) and model-matrix execution paths.

Held apart from the single-trial ``run`` body in :mod:`teatree.cli.eval.app`: a
multi-trial / matrix run always drives the metered in-process sdk runner and
aggregates across trials/models, a distinct concern from the default
single-pass grade.
"""

import json
import sys

import typer

from teatree.cli.eval.run_modes import (
    RunGuards,
    gate_run_regressions,
    persist_matrix_run,
    persist_pass_at_k_run,
    with_model,
)
from teatree.eval.matrix import MatrixRow, render_matrix_json, render_matrix_text
from teatree.eval.models import EvalSpec
from teatree.eval.pass_at_k import run_pass_at_k
from teatree.eval.report import ScenarioResult, evaluate
from teatree.eval.sdk_runner import SdkInProcessRunner


def run_pass_at_k_lane(  # noqa: PLR0913 — each kwarg threads one `eval run` CLI flag through the pass@k path.
    specs: list[EvalSpec],
    *,
    max_turns: int | None,
    trials: int,
    require: str,
    output_format: str,
    persist: bool = False,
    baseline: bool = False,
    gate_regressions: bool = False,
    model_override: str | None = None,
    grader=None,  # noqa: ANN001 — JudgeGrader | None, kept local to the CLI.
    require_executed: bool = False,
) -> bool:
    """Run the pass@k path; return ``True`` when any scenario failed or regressed."""
    if require not in {"any", "all"}:
        typer.echo(f"unknown --require {require!r}; use 'any' or 'all'", err=True)
        raise typer.Exit(code=2)
    runner = SdkInProcessRunner(max_turns_override=max_turns, require_executed=require_executed)

    def _trial(spec: EvalSpec) -> ScenarioResult:
        return evaluate(spec, runner.run(spec), judge=grader)

    effective_specs = [with_model(spec, model_override) for spec in specs] if model_override else specs
    results = [run_pass_at_k(spec, _trial, k=trials, require=require) for spec in effective_specs]
    if output_format == "json":
        typer.echo(
            json.dumps(
                {
                    "mode": f"pass@{trials}" if require == "any" else f"pass^{trials}",
                    "scenarios": [
                        {
                            "name": r.spec_name,
                            "trials": r.trials,
                            "passes": r.passes,
                            "pass_rate": r.pass_rate,
                            "skipped": r.skipped,
                            "ok": r.ok,
                        }
                        for r in results
                    ],
                },
                indent=2,
            )
        )
    else:
        for r in results:
            if r.skipped:
                typer.echo(f"SKIP {r.spec_name}: all {r.trials} trials skipped")
                continue
            status = "PASS" if r.ok else "FAIL"
            typer.echo(f"{status} {r.spec_name} ({r.passes}/{r.trials} trials, require={r.require})")
    RunGuards.executed(
        executed=sum(1 for r in results if not r.skipped), collected=len(specs), required=require_executed
    )
    regressed = False
    if persist:
        model_name = model_override or (effective_specs[0].model if effective_specs else "")
        record = persist_pass_at_k_run(results, model=model_name, max_turns=max_turns, baseline=baseline)
        regressed = gate_run_regressions(record, enabled=gate_regressions)
    failed = any(not r.ok for r in results) or regressed
    if failed and model_override is None:
        sys.exit(1)
    return failed


def run_model_matrix_lane(  # noqa: PLR0913 — each kwarg threads one `eval run` CLI flag through the matrix path.
    specs: list[EvalSpec],
    *,
    models: str,
    max_turns: int | None,
    trials: int,
    require: str,
    output_format: str,
    persist: bool,
    baseline: bool,
    gate_regressions: bool,
    grader=None,  # noqa: ANN001 — JudgeGrader | None, kept local to the CLI.
    require_executed: bool = False,
) -> None:
    """Run the suite once per model and render a per-model comparison."""
    model_list = [m.strip() for m in models.split(",") if m.strip()]
    if not model_list:
        typer.echo("--models was empty; pass e.g. --models opus,sonnet,haiku", err=True)
        raise typer.Exit(code=2)
    runner = SdkInProcessRunner(max_turns_override=max_turns, require_executed=require_executed)
    rows: list[MatrixRow] = []
    for model in model_list:
        for spec in specs:
            scoped = with_model(spec, model)
            rows.append(_matrix_trial(runner, scoped, trials=trials, require=require, grader=grader))
    if output_format == "json":
        typer.echo(render_matrix_json(rows, model_list, specs))
    else:
        typer.echo(render_matrix_text(rows, model_list, specs))
    RunGuards.executed(
        executed=sum(1 for row in rows if not row.skipped), collected=len(rows), required=require_executed
    )
    regressed = False
    if persist:
        record = persist_matrix_run(rows, models=model_list, max_turns=max_turns, baseline=baseline)
        regressed = gate_run_regressions(record, enabled=gate_regressions)
    if any(not row.passed and not row.skipped for row in rows) or regressed:
        sys.exit(1)


def _matrix_trial(
    runner: SdkInProcessRunner,
    spec: EvalSpec,
    *,
    trials: int,
    require: str,
    grader=None,  # noqa: ANN001 — JudgeGrader | None, kept local to the CLI.
) -> MatrixRow:
    if trials > 1:
        result = run_pass_at_k(spec, lambda s: evaluate(s, runner.run(s), judge=grader), k=trials, require=require)
        return MatrixRow(
            scenario=spec.name,
            model=spec.model,
            passed=result.ok and not result.skipped,
            score=0.0 if result.skipped else result.pass_rate,
            trials=result.trials,
            skipped=result.skipped,
        )
    scenario_result = evaluate(spec, runner.run(spec), judge=grader)
    return MatrixRow(
        scenario=spec.name,
        model=spec.model,
        passed=scenario_result.passed and not scenario_result.skipped,
        score=0.0 if scenario_result.skipped else (1.0 if scenario_result.passed else 0.0),
        trials=1,
        skipped=scenario_result.skipped,
    )
