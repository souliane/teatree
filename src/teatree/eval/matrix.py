"""Model-regression matrix: the per-model comparison value object and renderers.

A matrix run executes the scenario suite once per model in a configurable model
list and collects one :class:`MatrixRow` per ``(scenario, model)`` cell. This
module owns the row shape and the text/JSON rendering so the CLI command stays a
thin orchestrator over the runner and the run-store. The rendering is pure
(rows + model order + spec order in, string out), so it is unit-testable without
touching ``claude -p`` or the DB.
"""

import dataclasses
import json

from teatree.eval.models import EvalSpec, TokenUsage


@dataclasses.dataclass(frozen=True)
class MatrixRow:
    """One scenario's verdict against one model in a matrix run."""

    scenario: str
    model: str
    passed: bool
    score: float
    trials: int
    skipped: bool
    #: Total metered cost for this cell (summed across trials; 0.0 for a
    #: non-metered run) — the cost-regression gate's per-scenario substrate.
    cost_usd: float = 0.0
    #: The runner raised an unexpected (infra/transient) exception for this cell
    #: even after the bounded retries — DISTINCT from a graded ``passed=False``
    #: FAIL (the agent did not satisfy the matchers) and from ``skipped`` (not
    #: provisioned). An errored cell keeps ``passed=False``/``skipped=False`` and
    #: is excluded from the pass-rate so a transient blip never lowers a model's
    #: measured score.
    errored: bool = False
    #: This cell's token usage (summed across trials, mirroring ``cost_usd``;
    #: all-zero for a non-metered/subscription run or an errored cell) — the
    #: substrate for the benchmark's honest cache-cost columns.
    usage: TokenUsage = dataclasses.field(default_factory=TokenUsage)
    #: The cell's billed model differs from the requested variant's base model —
    #: ``fallback_model`` kicked in, so the billed cost mixes model rates. The
    #: benchmark surfaces this so a fallen-back cell's cost isn't read as the
    #: requested model's. Unobservable (subscription/offline) cells are ``False``.
    fell_back: bool = False
    #: The run's terminal reason (``success``/``end_turn`` for a clean finish,
    #: ``budget_exceeded``/``max_turns``/``timeout``/``error_*`` for a cap). The
    #: warm-equivalent fit excludes cap-truncated cells, whose billed cost does
    #: not match the clean identity and would bias the fit.
    terminal_reason: str = ""
    #: This cell's metered cost split into the requested MAIN model vs the
    #: AUXILIARY background (Claude Code's ``claude-haiku-4-5``), summed across
    #: trials. ``0.0`` for a non-metered/errored/skipped cell. The benchmark
    #: surfaces the main cost as the headline comparison and the aux separately.
    main_cost_usd: float = 0.0
    aux_cost_usd: float = 0.0
    #: This cell's MAIN-model and AUXILIARY token usage (summed across trials).
    main_usage: TokenUsage = dataclasses.field(default_factory=TokenUsage)
    aux_usage: TokenUsage = dataclasses.field(default_factory=TokenUsage)


def matrix_cell(row: MatrixRow | None) -> str:
    """Render one table cell: ``pass``/``FAIL``/``ERR``/``skip``/``-`` (or a pass-rate)."""
    if row is None:
        return "-"
    if row.errored:
        return "ERR"
    if row.skipped:
        return "skip"
    if row.trials > 1:
        return f"{row.score:.2f}" if row.passed else f"FAIL({row.score:.2f})"
    return "pass" if row.passed else "FAIL"


def render_matrix_text(rows: list[MatrixRow], models: list[str], specs: list[EvalSpec]) -> str:
    """Render a scenario-by-model table followed by a per-model pass/fail tally."""
    by_key = {(r.scenario, r.model): r for r in rows}
    scenario_names = [s.name for s in specs]
    name_width = max((len(n) for n in scenario_names), default=8)
    col_width = max(8, *(len(m) for m in models))
    header = "scenario".ljust(name_width) + "  " + "  ".join(m.ljust(col_width) for m in models)
    lines = [header, "-" * len(header)]
    for name in scenario_names:
        cells = [matrix_cell(by_key.get((name, m))).ljust(col_width) for m in models]
        lines.append(name.ljust(name_width) + "  " + "  ".join(cells))
    lines.append("")
    for model in models:
        model_rows = [r for r in rows if r.model == model]
        passed = sum(1 for r in model_rows if r.passed and not r.skipped)
        # An errored cell is neither a pass nor a graded FAIL — exclude it from
        # "failed" so a transient infra blip never inflates the failure count.
        failed = sum(1 for r in model_rows if not r.passed and not r.skipped and not r.errored)
        skipped = sum(1 for r in model_rows if r.skipped)
        errored = sum(1 for r in model_rows if r.errored)
        lines.append(f"{model}: {passed} passed, {failed} failed, {skipped} skipped, {errored} errored")
    return "\n".join(lines)


def render_matrix_json(rows: list[MatrixRow], models: list[str], specs: list[EvalSpec]) -> str:
    """Render the matrix as ``{models, scenarios:[{name, results:{model:{...}}}]}``."""
    by_key = {(r.scenario, r.model): r for r in rows}
    payload = {
        "models": models,
        "scenarios": [
            {
                "name": spec.name,
                "results": {
                    model: (
                        None
                        if (cell := by_key.get((spec.name, model))) is None
                        else {
                            "passed": cell.passed,
                            "score": cell.score,
                            "trials": cell.trials,
                            "skipped": cell.skipped,
                            "errored": cell.errored,
                        }
                    )
                    for model in models
                },
            }
            for spec in specs
        ],
    }
    return json.dumps(payload, indent=2)
