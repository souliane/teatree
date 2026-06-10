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
    DEFAULT_COST_REGRESSION_TOLERANCE,
    RegressionGates,
    RunGuards,
    persist_matrix_run,
    persist_pass_at_k_run,
    with_model,
)
from teatree.eval.matrix import MatrixRow, render_matrix_json, render_matrix_text
from teatree.eval.model_variant import ModelVariantError, parse_model_variants
from teatree.eval.models import EvalSpec
from teatree.eval.pass_at_k import run_pass_at_k
from teatree.eval.report import ScenarioResult, evaluate
from teatree.eval.sdk_runner import MAX_BUDGET_USD, SdkInProcessRunner

#: How many extra attempts a single matrix/benchmark cell gets after its first
#: failure. A clean-room scenario is idempotent (re-running costs only extra
#: metered $), so a bounded retry rides out a transient CLI non-zero exit —
#: ``MAX_MATRIX_CELL_RETRIES + 1`` attempts total before the cell is recorded
#: ERRORED so the rest of the comparison table is still produced.
MAX_MATRIX_CELL_RETRIES = 2


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
    gate_cost_regression: bool = False,
    cost_regression_tolerance: float = DEFAULT_COST_REGRESSION_TOLERANCE,
    model_override: str | None = None,
    grader=None,  # noqa: ANN001 — JudgeGrader | None, kept local to the CLI.
    require_executed: bool = False,
    max_budget_usd: float = float(MAX_BUDGET_USD),
) -> bool:
    """Run the pass@k path; return ``True`` when any scenario failed or regressed."""
    if require not in {"any", "all"}:
        typer.echo(f"unknown --require {require!r}; use 'any' or 'all'", err=True)
        raise typer.Exit(code=2)
    runner = SdkInProcessRunner(
        max_turns_override=max_turns, require_executed=require_executed, max_budget_usd=max_budget_usd
    )

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
    cost_regressed = False
    if persist:
        model_name = model_override or (effective_specs[0].model if effective_specs else "")
        record = persist_pass_at_k_run(results, model=model_name, max_turns=max_turns, baseline=baseline)
        regressed = RegressionGates.scores(record, enabled=gate_regressions)
        cost_regressed = RegressionGates.costs(
            record, enabled=gate_cost_regression, tolerance=cost_regression_tolerance
        )
    failed = any(not r.ok for r in results) or regressed or cost_regressed
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
    gate_cost_regression: bool = False,
    cost_regression_tolerance: float = DEFAULT_COST_REGRESSION_TOLERANCE,
    grader=None,  # noqa: ANN001 — JudgeGrader | None, kept local to the CLI.
    require_executed: bool = False,
    max_budget_usd: float = float(MAX_BUDGET_USD),
) -> None:
    """Run the suite once per model and render a per-model comparison."""
    model_list = parse_model_tags(models)
    runner = SdkInProcessRunner(
        max_turns_override=max_turns, require_executed=require_executed, max_budget_usd=max_budget_usd
    )
    rows = collect_matrix_rows(specs, model_list, runner=runner, trials=trials, require=require, grader=grader)
    if output_format == "json":
        typer.echo(render_matrix_json(rows, model_list, specs))
    else:
        typer.echo(render_matrix_text(rows, model_list, specs))
    RunGuards.executed(
        executed=sum(1 for row in rows if not row.skipped), collected=len(rows), required=require_executed
    )
    regressed = False
    cost_regressed = False
    if persist:
        record = persist_matrix_run(rows, models=model_list, max_turns=max_turns, baseline=baseline)
        regressed = RegressionGates.scores(record, enabled=gate_regressions)
        cost_regressed = RegressionGates.costs(
            record, enabled=gate_cost_regression, tolerance=cost_regression_tolerance
        )
    # An errored cell is NOT a graded FAIL, but the lane still exits non-zero on
    # it for visibility — a transient blip should be seen, just not counted as a
    # model failure in the comparison.
    failed = any(not row.passed and not row.skipped and not row.errored for row in rows)
    errored = any(row.errored for row in rows)
    if failed or errored or regressed or cost_regressed:
        sys.exit(1)


def parse_model_tags(models: str) -> list[str]:
    """Parse ``--models`` into validated variant tags, or exit 2 with the parse error.

    Each entry is a ``model[@effort]`` variant (`teatree.eval.model_variant`);
    the rendered tag is the identity string the matrix/benchmark machinery
    threads through ``MatrixRow.model`` and the run-store ledger.
    """
    try:
        variants = parse_model_variants(models)
    except ModelVariantError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from None
    if not variants:
        typer.echo("--models was empty; pass e.g. --models opus,sonnet,haiku", err=True)
        raise typer.Exit(code=2)
    return [variant.tag for variant in variants]


def collect_matrix_rows(  # noqa: PLR0913 — each kwarg threads one matrix/benchmark CLI flag through the shared loop.
    specs: list[EvalSpec],
    model_tags: list[str],
    *,
    runner: SdkInProcessRunner,
    trials: int,
    require: str,
    grader=None,  # noqa: ANN001 — JudgeGrader | None, kept local to the CLI.
) -> list[MatrixRow]:
    """Run every scenario against every variant tag — the shared matrix/benchmark loop.

    Each cell goes through :func:`_resilient_matrix_trial`, so one cell's
    transient runner exception is isolated (retried, then recorded as an ERRORED
    row) and never aborts the whole comparison — the full table is always
    produced.
    """
    return [
        _resilient_matrix_trial(runner, with_model(spec, tag), trials=trials, require=require, grader=grader)
        for tag in model_tags
        for spec in specs
    ]


def _resilient_matrix_trial(
    runner: SdkInProcessRunner,
    spec: EvalSpec,
    *,
    trials: int,
    require: str,
    grader=None,  # noqa: ANN001 — JudgeGrader | None, kept local to the CLI.
) -> MatrixRow:
    """Run one cell with bounded retries; on persistent failure, an ERRORED row.

    Only an *unexpected* ``Exception`` from the runner is caught — a genuine SDK
    error the single-scenario ``t3 eval run`` path re-raises (``run()`` keeps
    that fail-loud). ``KeyboardInterrupt``/``SystemExit`` are ``BaseException``s
    and propagate; ``typer.Exit`` subclasses ``RuntimeError`` (an ``Exception``)
    but is a control-flow signal, so it is re-raised explicitly rather than
    isolated. (``_TerminalResultError``/``TimeoutError`` are already handled
    inside ``run()`` and never reach here.) After :data:`MAX_MATRIX_CELL_RETRIES`
    retries still fail, the cell is logged loudly to stderr and recorded
    ``errored=True`` so the rest of the matrix survives.
    """
    last_exc: Exception | None = None
    for attempt in range(MAX_MATRIX_CELL_RETRIES + 1):
        try:
            return _matrix_trial(runner, spec, trials=trials, require=require, grader=grader)
        except typer.Exit:
            raise
        except Exception as exc:  # noqa: BLE001 — isolate THIS cell; genuine errors already re-raised in run().
            last_exc = exc
            print(  # noqa: T201 — loud per-attempt visibility on stderr, never swallowed.
                f"WARNING cell {spec.name} @ {spec.model} attempt {attempt + 1}/"
                f"{MAX_MATRIX_CELL_RETRIES + 1} raised: {exc}",
                file=sys.stderr,
            )
    print(  # noqa: T201 — give-up record is loud; the cell becomes ERRORED, not lost.
        f"ERROR cell {spec.name} @ {spec.model} failed after {MAX_MATRIX_CELL_RETRIES + 1} attempts: {last_exc}",
        file=sys.stderr,
    )
    return MatrixRow(
        scenario=spec.name,
        model=spec.model,
        passed=False,
        score=0.0,
        trials=1,
        skipped=False,
        cost_usd=0.0,
        errored=True,
    )


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
            cost_usd=result.cost_usd,
        )
    scenario_result = evaluate(spec, runner.run(spec), judge=grader)
    return MatrixRow(
        scenario=spec.name,
        model=spec.model,
        passed=scenario_result.passed and not scenario_result.skipped,
        score=0.0 if scenario_result.skipped else (1.0 if scenario_result.passed else 0.0),
        trials=1,
        skipped=scenario_result.skipped,
        cost_usd=scenario_result.run.cost_usd,
    )
