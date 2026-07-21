"""``t3 eval run`` multi-trial (pass@k) and model-matrix execution paths.

Held apart from the single-trial ``run`` body in :mod:`teatree.cli.eval.app`: a
multi-trial / matrix run always drives the metered in-process api runner and
aggregates across trials/models, a distinct concern from the default
single-pass grade.
"""

import dataclasses
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

import typer
from claude_agent_sdk.types import EffortLevel

from teatree.cli.eval.run_modes import (
    DEFAULT_COST_REGRESSION_TOLERANCE,
    CostBoundsGate,
    RegressionGates,
    RunGuards,
    persist_matrix_run,
    persist_pass_at_k_run,
    require_persist_for_history_gates,
    with_model,
)
from teatree.eval.api_runner import MAX_BUDGET_USD
from teatree.eval.backends import API_BACKEND, ApiRunnerParams, EvalRunner, make_runner
from teatree.eval.matrix import MatrixRow, render_matrix_html, render_matrix_json, render_matrix_text
from teatree.eval.model_resolution import resolve_eval_model
from teatree.eval.model_variant import ModelVariantError, parse_model_variants
from teatree.eval.models import EvalSpec
from teatree.eval.pass_at_k import PassAtKResult, run_pass_at_k
from teatree.eval.pass_at_k_html import render_pass_at_k_html
from teatree.eval.presets import Preset, PresetError, resolve_preset, resolve_preset_model
from teatree.eval.report import ScenarioResult, evaluate, render_summary_markdown
from teatree.eval.summary_json import write_summary_json

#: The column name ``--presets`` recognises for "no preset" — each scenario's
#: own tier/phase, the same resolution ``t3 eval run`` uses with no ``--preset``
#: active. Distinct from any registered :class:`~teatree.eval.presets.Preset`
#: name, so it never collides with ``cheap``/``frontier``/``baseline``.
DEFAULT_PRESET_COLUMN_NAME = "default"

#: How many extra attempts a single matrix/benchmark cell gets after its first
#: failure. A clean-room scenario is idempotent (re-running costs only extra
#: metered $), so a bounded retry rides out a transient CLI non-zero exit —
#: ``MAX_MATRIX_CELL_RETRIES + 1`` attempts total before the cell is recorded
#: ERRORED so the rest of the comparison table is still produced.
MAX_MATRIX_CELL_RETRIES = 2


def _write_pass_at_k_artifacts(
    results: list[PassAtKResult],
    *,
    transcript_html: Path | None,
    summary_md: Path | None,
    summary_json: Path | None,
) -> None:
    """Drop the per-trial transcript + sanitized dashboards BEFORE any guard/gate exits.

    Each is written from THIS run's in-memory results so a red lane still drops the
    diagnostic transcript AND the publish-safe summary/JSON the workflow uploads.
    """
    if transcript_html is not None:
        transcript_html.write_text(render_pass_at_k_html(results), encoding="utf-8")
    if summary_md is not None:
        summary_md.write_text(render_summary_markdown(results), encoding="utf-8")
    if summary_json is not None:
        write_summary_json(results, summary_json)


def _emit_progress(line: str) -> None:
    """Print one flushed progress line to stderr so the CI log streams it live.

    The metered suite runs each scenario inside a silent list-comprehension; with
    no per-scenario emission a hang produces ZERO output until the whole suite
    ends (and GitHub never exposes an in-progress job's log blob), so a hung run
    is indistinguishable from a slow one. A flushed ``RUN``/``DONE`` line per
    scenario streams to the runner's stdout in real time: the suite is visibly
    advancing, and a hang leaves the last ``RUN <scenario>`` line as the pinpoint.
    """
    print(line, file=sys.stderr, flush=True)  # noqa: T201 — load-bearing live progress; see docstring.


def _run_scenario_with_progress(
    spec: EvalSpec,
    trial: Callable[[EvalSpec], ScenarioResult],
    *,
    trials: int,
    require: str,
    position: tuple[int, int],
) -> PassAtKResult:
    """Run one pass@k scenario, bracketed by flushed RUN/DONE progress lines."""
    index, total = position
    _emit_progress(f"RUN  [{index}/{total}] {spec.name} (k={trials}, require={require})")
    result = run_pass_at_k(spec, trial, k=trials, require=require)
    verdict = "SKIP" if result.skipped else ("PASS" if result.ok else "FAIL")
    _emit_progress(f"DONE [{index}/{total}] {spec.name}: {verdict} ({result.passes}/{result.trials})")
    return result


# ast-grep-ignore: ac-django-no-complexity-suppressions
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
    gate_cost_bounds: bool = False,
    model_override: str | None = None,
    grader=None,  # noqa: ANN001 — JudgeGrader | None, kept local to the CLI.
    require_executed: bool = False,
    max_budget_usd: float = float(MAX_BUDGET_USD),
    effort: EffortLevel | None = None,
    transcript_html: Path | None = None,
    summary_md: Path | None = None,
    summary_json: Path | None = None,
) -> bool:
    """Run the pass@k path; return ``True`` when any scenario failed or regressed.

    ``effort`` is the resolved lane-level reasoning effort (the ``--effort`` /
    ``METERED_DEFAULT_EFFORT`` calibration). It is the runner-wide default applied
    to scenarios that declare no ``model@effort`` of their own; a scenario's own
    tag still wins at the runner's per-scenario seam.

    ``transcript_html`` is a writable path to drop a self-contained per-trial
    transcript report at — the durable, uploadable artifact a maintainer reads to
    diagnose a red lane. It is written from THIS run's in-memory results (no suite
    re-run, no ledger), so it survives the ``--no-persist`` ephemeral-container
    CI path where nothing reaches the host run-history.

    ``summary_md`` is a writable path to drop the SANITIZED aggregate dashboard
    markdown at (counts + cost + a ``scenario | lane | verdict | trials | cost`` table,
    NO transcript) — the publish-safe sibling of ``transcript_html`` the weekly
    dashboard and the PR step-summary consume.
    """
    if require not in {"any", "all"}:
        typer.echo(f"unknown --require {require!r}; use 'any' or 'all'", err=True)
        raise typer.Exit(code=2)
    require_persist_for_history_gates(
        persist=persist,
        baseline=baseline,
        gate_regressions=gate_regressions,
        gate_cost_regression=gate_cost_regression,
        gate_cost_bounds=gate_cost_bounds,
    )
    runner = make_runner(
        API_BACKEND,
        ApiRunnerParams(
            max_turns_override=max_turns,
            require_executed=require_executed,
            max_budget_usd=max_budget_usd,
            effort=effort,
        ),
    )

    def _trial(spec: EvalSpec) -> ScenarioResult:
        return evaluate(spec, runner.run(spec), judge=grader)

    effective_specs = [with_model(spec, model_override) for spec in specs] if model_override else specs
    total = len(effective_specs)
    results = [
        _run_scenario_with_progress(spec, _trial, trials=trials, require=require, position=(index, total))
        for index, spec in enumerate(effective_specs, start=1)
    ]
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
    _write_pass_at_k_artifacts(
        results, transcript_html=transcript_html, summary_md=summary_md, summary_json=summary_json
    )
    RunGuards.executed(
        executed=sum(1 for r in results if not r.skipped), collected=len(specs), required=require_executed
    )
    RunGuards.api_metered_total(
        backend=API_BACKEND,
        executed=sum(1 for r in results if not r.skipped),
        total_cost_usd=sum(r.cost_usd for r in results),
    )
    regressed = False
    cost_regressed = False
    cost_bounds_failed = False
    if persist:
        model_name = model_override or (effective_specs[0].model if effective_specs else "")
        record = persist_pass_at_k_run(results, model=model_name, max_turns=max_turns, baseline=baseline)
        regressed = RegressionGates.scores(record, enabled=gate_regressions)
        cost_regressed = RegressionGates.costs(
            record, enabled=gate_cost_regression, tolerance=cost_regression_tolerance
        )
        cost_bounds_failed = CostBoundsGate.check(record, enabled=gate_cost_bounds)
    # Every scenario that failed reds the lane — there is no known-red allowance
    # and no metered ratchet. An under_load behavioural-drift failure is a real
    # failure exactly like any clean_room failure: a red scenario fails the run,
    # full stop.
    failed = any(not r.ok for r in results) or regressed or cost_regressed or cost_bounds_failed
    if failed and model_override is None:
        sys.exit(1)
    return failed


# ast-grep-ignore: ac-django-no-complexity-suppressions
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
    gate_cost_bounds: bool = False,
    grader=None,  # noqa: ANN001 — JudgeGrader | None, kept local to the CLI.
    require_executed: bool = False,
    max_budget_usd: float = float(MAX_BUDGET_USD),
    effort: EffortLevel | None = None,
    html_out: Path | None = None,
) -> None:
    """Run the suite once per model and render a per-model comparison.

    ``effort`` is the resolved lane-level reasoning effort (the ``--effort`` /
    ``METERED_DEFAULT_EFFORT`` calibration), the runner-wide default for scenarios
    declaring no ``model@effort``. A matrix variant's own ``model@effort`` tag (and
    a scenario's own) still win over this lane default at the runner's seam.

    ``html_out`` (the ``--benchmark`` artifact path) writes a self-contained HTML
    matrix dashboard from THIS run's rows, BEFORE any gate can exit — so a red
    benchmark still drops the publishable artifact the weekly workflow uploads.
    """
    model_list = parse_model_tags(models)
    require_persist_for_history_gates(
        persist=persist,
        baseline=baseline,
        gate_regressions=gate_regressions,
        gate_cost_regression=gate_cost_regression,
        gate_cost_bounds=gate_cost_bounds,
    )
    runner = make_runner(
        API_BACKEND,
        ApiRunnerParams(
            max_turns_override=max_turns,
            require_executed=require_executed,
            max_budget_usd=max_budget_usd,
            effort=effort,
        ),
    )
    rows = collect_matrix_rows(
        specs, model_list, runner=runner, policy=TrialPolicy(trials=trials, require=require), grader=grader
    )
    if output_format == "json":
        typer.echo(render_matrix_json(rows, model_list, specs))
    else:
        typer.echo(render_matrix_text(rows, model_list, specs))
    # The benchmark HTML artifact, written BEFORE any guard/gate can exit so a red
    # benchmark still drops the dashboard the weekly workflow uploads/publishes.
    if html_out is not None:
        html_out.write_text(render_matrix_html(rows, model_list, specs), encoding="utf-8")
    RunGuards.executed(
        executed=sum(1 for row in rows if not row.skipped), collected=len(rows), required=require_executed
    )
    regressed = False
    cost_regressed = False
    cost_bounds_failed = False
    if persist:
        record = persist_matrix_run(rows, models=model_list, max_turns=max_turns, baseline=baseline)
        regressed = RegressionGates.scores(record, enabled=gate_regressions)
        cost_regressed = RegressionGates.costs(
            record, enabled=gate_cost_regression, tolerance=cost_regression_tolerance
        )
        cost_bounds_failed = CostBoundsGate.check(record, enabled=gate_cost_bounds)
    # An errored cell is NOT a graded FAIL, but the lane still exits non-zero on
    # it for visibility — a transient blip should be seen, just not counted as a
    # model failure in the comparison.
    failed = any(not row.passed and not row.skipped and not row.errored for row in rows)
    errored = any(row.errored for row in rows)
    if failed or errored or regressed or cost_regressed or cost_bounds_failed:
        sys.exit(1)


@dataclasses.dataclass(frozen=True)
class MatrixColumn:
    """One matrix/benchmark column: a display ``tag`` + how it resolves a spec's model.

    A plain model/tag column (``--models opus,sonnet``) resolves every spec to
    the SAME tag (:func:`_uniform_column`). A PRESET column
    (:func:`preset_column`) resolves each spec through the preset's own
    precedence — two scenarios under the same preset ``tag`` (e.g.
    ``"baseline"``) can genuinely run different models. ``MatrixRow.model`` is
    stamped with ``tag`` (never the per-spec resolved model), so grouping by
    ``row.model`` still identifies the COLUMN, not the incidental model a given
    scenario happened to run under it.
    """

    tag: str
    model_for: Callable[[EvalSpec], str]


def _uniform_column(tag: str) -> MatrixColumn:
    """A column that resolves every spec to the same *tag* — the plain ``--models`` shape."""
    return MatrixColumn(tag=tag, model_for=lambda _spec: tag)


def preset_column(preset: Preset) -> MatrixColumn:
    """A column that resolves each spec through *preset* — the ``--presets`` shape."""
    return MatrixColumn(tag=preset.name, model_for=lambda spec: resolve_preset_model(spec, preset))


def _default_column() -> MatrixColumn:
    """The ``--presets ...,default,...`` column: each scenario's own tier/phase, no preset."""
    return MatrixColumn(tag=DEFAULT_PRESET_COLUMN_NAME, model_for=resolve_eval_model)


def _as_column(column: "str | MatrixColumn") -> MatrixColumn:
    return column if isinstance(column, MatrixColumn) else _uniform_column(column)


def parse_preset_columns(presets: str) -> list[MatrixColumn]:
    """Parse ``--presets`` (comma-separated preset names) into matrix columns, or exit 2.

    Each name resolves to a :class:`MatrixColumn` via :func:`preset_column`,
    except :data:`DEFAULT_PRESET_COLUMN_NAME` (``"default"``) — the no-preset
    baseline comparison column, each scenario's own tier/phase.
    """
    names = [name.strip() for name in presets.split(",") if name.strip()]
    if not names:
        typer.echo(f"--presets was empty; pass e.g. --presets cheap,baseline,{DEFAULT_PRESET_COLUMN_NAME}", err=True)
        raise typer.Exit(code=2)
    columns: list[MatrixColumn] = []
    for name in names:
        if name == DEFAULT_PRESET_COLUMN_NAME:
            columns.append(_default_column())
            continue
        try:
            columns.append(preset_column(resolve_preset(name)))
        except PresetError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from None
    return columns


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


@dataclasses.dataclass(frozen=True)
class TrialPolicy:
    """How many times to run each cell and what counts as a pass — the pass@k knobs."""

    trials: int = 1
    require: str = "any"


@dataclasses.dataclass(frozen=True)
class _MatrixCell:
    """One matrix cell to execute: *spec* (its column model already applied) + the column's *tag*."""

    spec: EvalSpec
    tag: str
    policy: TrialPolicy


def collect_matrix_rows(
    specs: list[EvalSpec],
    columns: "Sequence[str | MatrixColumn]",
    *,
    runner: EvalRunner,
    policy: TrialPolicy,
    grader=None,  # noqa: ANN001 — JudgeGrader | None, kept local to the CLI.
) -> list[MatrixRow]:
    """Run every scenario against every column — the shared matrix/benchmark loop.

    *columns* accepts a plain model/tag string (wrapped via
    :func:`_uniform_column`, the existing ``--models``/``--benchmark`` shape) or
    an explicit :class:`MatrixColumn` (the ``--presets`` shape, where each
    scenario may resolve to a different concrete model under the same column).
    Each cell goes through :func:`_resilient_matrix_trial`, so one cell's
    transient runner exception is isolated (retried, then recorded as an ERRORED
    row) and never aborts the whole comparison — the full table is always
    produced.
    """
    return [
        _resilient_matrix_trial(
            runner,
            _MatrixCell(spec=with_model(spec, column.model_for(spec)), tag=column.tag, policy=policy),
            grader=grader,
        )
        for column in (_as_column(column) for column in columns)
        for spec in specs
    ]


def _resilient_matrix_trial(
    runner: EvalRunner,
    cell: _MatrixCell,
    *,
    grader=None,  # noqa: ANN001 — JudgeGrader | None, kept local to the CLI.
) -> MatrixRow:
    """Run one cell with bounded retries; on persistent failure, an ERRORED row.

    Only an *unexpected* ``Exception`` from the runner is caught — a genuine SDK
    error the single-scenario ``t3 eval run`` path re-raises (``run()`` keeps
    that fail-loud). ``KeyboardInterrupt``/``SystemExit`` are ``BaseException``s
    and propagate; ``typer.Exit`` subclasses ``RuntimeError`` (an ``Exception``)
    but is a control-flow signal, so it is re-raised explicitly rather than
    isolated. (``TerminalResultError``/``TimeoutError`` are already handled
    inside ``run()`` and never reach here.) After :data:`MAX_MATRIX_CELL_RETRIES`
    retries still fail, the cell is logged loudly to stderr and recorded
    ``errored=True`` so the rest of the matrix survives.
    """
    last_exc: Exception | None = None
    for attempt in range(MAX_MATRIX_CELL_RETRIES + 1):
        try:
            return _matrix_trial(runner, cell, grader=grader)
        except typer.Exit:
            raise
        except Exception as exc:  # noqa: BLE001 — isolate THIS cell; genuine errors already re-raised in run().
            last_exc = exc
            print(  # noqa: T201 — loud per-attempt visibility on stderr, never swallowed.
                f"WARNING cell {cell.spec.name} @ {cell.spec.model} attempt {attempt + 1}/"
                f"{MAX_MATRIX_CELL_RETRIES + 1} raised: {exc}",
                file=sys.stderr,
            )
    print(  # noqa: T201 — give-up record is loud; the cell becomes ERRORED, not lost.
        f"ERROR cell {cell.spec.name} @ {cell.spec.model} failed after "
        f"{MAX_MATRIX_CELL_RETRIES + 1} attempts: {last_exc}",
        file=sys.stderr,
    )
    return MatrixRow(
        scenario=cell.spec.name,
        model=cell.tag,
        passed=False,
        score=0.0,
        trials=1,
        skipped=False,
        cost_usd=0.0,
        errored=True,
    )


def _matrix_trial(
    runner: EvalRunner,
    cell: _MatrixCell,
    *,
    grader=None,  # noqa: ANN001 — JudgeGrader | None, kept local to the CLI.
) -> MatrixRow:
    spec = cell.spec
    if cell.policy.trials > 1:
        result = run_pass_at_k(
            spec, lambda s: evaluate(s, runner.run(s), judge=grader), k=cell.policy.trials, require=cell.policy.require
        )
        return MatrixRow(
            scenario=spec.name,
            model=cell.tag,
            passed=result.ok and not result.skipped,
            score=0.0 if result.skipped else result.pass_rate,
            trials=result.trials,
            skipped=result.skipped,
            cost_usd=result.cost_usd,
            usage=result.usage,
            fell_back=_fell_back(signal=result.fell_back),
            terminal_reason=result.terminal_reason,
            main_cost_usd=result.main_cost_usd,
            aux_cost_usd=result.aux_cost_usd,
            main_usage=result.main_usage,
            aux_usage=result.aux_usage,
        )
    scenario_result = evaluate(spec, runner.run(spec), judge=grader)
    run = scenario_result.run
    return MatrixRow(
        scenario=spec.name,
        model=cell.tag,
        passed=scenario_result.passed and not scenario_result.skipped,
        score=0.0 if scenario_result.skipped else (1.0 if scenario_result.passed else 0.0),
        trials=1,
        skipped=scenario_result.skipped,
        cost_usd=run.cost_usd,
        usage=run.usage,
        fell_back=_fell_back(signal=run.fell_back),
        terminal_reason=run.terminal_reason,
        main_cost_usd=run.main_cost_usd,
        aux_cost_usd=run.aux_cost_usd,
        main_usage=run.main_usage,
        aux_usage=run.aux_usage,
    )


def _fell_back(*, signal: bool | None) -> bool:
    """Collapse the run's requested-model-presence ``fell_back`` signal onto the cell.

    The run carries ``True`` (the requested main model was substituted), ``False``
    (it was present — a haiku auxiliary beside it is NORMAL, not a fallback), or
    ``None`` (subscription/offline — unobservable). An unobservable cell is NOT a
    fallback, so ``None`` collapses to ``False``.
    """
    return signal is True
