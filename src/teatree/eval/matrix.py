"""Model-regression matrix: the per-model comparison value object and renderers.

A matrix run executes the scenario suite once per model in a configurable model
list and collects one :class:`MatrixRow` per ``(scenario, model)`` cell. This
module owns the row shape and the text/JSON rendering so the CLI command stays a
thin orchestrator over the runner and the run-store. The rendering is pure
(rows + model order + spec order in, string out), so it is unit-testable without
touching ``claude -p`` or the DB.
"""

import dataclasses
import html
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


def render_matrix_html(rows: list[MatrixRow], models: list[str], specs: list[EvalSpec]) -> str:
    """Render a self-contained HTML matrix dashboard — the ``--benchmark`` artifact.

    One column per resolved tier model, one row per scenario, plus a per-model
    pass/fail/skip/error tally footer. Self-contained (inline styles) so the
    weekly workflow can upload it as one artifact and publish it directly.
    """
    by_key = {(r.scenario, r.model): r for r in rows}
    head_cells = "".join(f"<th>{html.escape(m)}</th>" for m in models)
    body_rows = []
    for spec in specs:
        cells = "".join(_html_cell(by_key.get((spec.name, m))) for m in models)
        body_rows.append(f"<tr><td class='scenario'>{html.escape(spec.name)}</td>{cells}</tr>")
    tally_rows = []
    for model in models:
        model_rows = [r for r in rows if r.model == model]
        passed = sum(1 for r in model_rows if r.passed and not r.skipped)
        failed = sum(1 for r in model_rows if not r.passed and not r.skipped and not r.errored)
        skipped = sum(1 for r in model_rows if r.skipped)
        errored = sum(1 for r in model_rows if r.errored)
        cost = sum(r.cost_usd for r in model_rows)
        tally_rows.append(
            f"<tr><td>{html.escape(model)}</td><td>{passed}</td><td>{failed}</td>"
            f"<td>{skipped}</td><td>{errored}</td><td>${cost:.4f}</td></tr>"
        )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Eval benchmark matrix</title><style>"
        "body{font-family:system-ui,sans-serif;margin:2rem}"
        "table{border-collapse:collapse;margin-bottom:2rem}"
        "th,td{border:1px solid #ccc;padding:4px 8px;text-align:center;font-size:13px}"
        "td.scenario{text-align:left;font-family:monospace}"
        ".pass{background:#d7f7d0}.fail{background:#f7d0d0}.skip{background:#eee}.err{background:#f7e0b0}"
        "</style></head><body><h1>Eval benchmark matrix</h1>"
        f"<table><thead><tr><th>scenario</th>{head_cells}</tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody></table>"
        "<h2>Per-model tally</h2><table><thead><tr><th>model</th><th>passed</th>"
        "<th>failed</th><th>skipped</th><th>errored</th><th>cost</th></tr></thead>"
        f"<tbody>{''.join(tally_rows)}</tbody></table></body></html>"
    )


def _html_cell(row: MatrixRow | None) -> str:
    """One HTML matrix cell, colour-coded by verdict."""
    if row is None:
        return "<td>-</td>"
    if row.errored:
        return "<td class='err'>ERR</td>"
    if row.skipped:
        return "<td class='skip'>skip</td>"
    label = matrix_cell(row)
    klass = "pass" if row.passed else "fail"
    return f"<td class='{klass}'>{html.escape(label)}</td>"


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
