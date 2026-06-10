"""``t3 eval benchmark`` — cost/pass-rate comparison across ``model@effort`` variants.

A thin command over the model-matrix machinery (`teatree.cli.eval.multi_trial`):
it runs the suite once per variant on the metered in-process Agent-SDK runner
(``--backend sdk`` semantics, all-skipped gate always armed), persists the
matrix record, and folds the rows into the per-variant comparison table in
:mod:`teatree.eval.benchmark` — the deliverable for "how does opus@xhigh
compare to fable@medium on pass-rate and cost".
"""

import typer

from teatree.cli._format_opts import require_valid_format
from teatree.cli.eval.multi_trial import collect_matrix_rows, parse_model_tags
from teatree.cli.eval.run_modes import RunGuards, persist_matrix_run
from teatree.eval.benchmark import render_benchmark_json, render_benchmark_text, summarize_benchmark
from teatree.eval.discovery import discover_specs
from teatree.eval.models import EvalSpec
from teatree.eval.sdk_runner import SdkInProcessRunner
from teatree.utils.django_bootstrap import ensure_django


def benchmark(  # noqa: PLR0913, PLR0917 — typer command: each param maps 1:1 to a public ``t3 eval benchmark`` flag.
    models: str = typer.Option(
        ...,
        "--models",
        help=(
            "Comma-separated model@effort variants to compare, e.g. "
            "claude-opus-4-8@xhigh,claude-fable-5@medium (a plain model name = default effort)."
        ),
    ),
    scenarios: str | None = typer.Option(
        None,
        "--scenarios",
        help="Comma-separated scenario names to benchmark (default: the whole suite).",
    ),
    trials: int = typer.Option(1, "--trials", help="Re-run each (scenario, variant) cell this many times."),
    max_turns: int | None = typer.Option(
        None,
        "--max-turns",
        help="Override every scenario's max_turns (per-invocation).",
    ),
    output_format: str = typer.Option("text", "--format", help="Report format: text or json."),
    persist: bool = typer.Option(  # noqa: FBT001 — typer boolean flag, not a positional bool foot-gun.
        True,
        "--persist/--no-persist",
        help="Persist the underlying matrix run into the run-history ledger (`t3 eval history`).",
    ),
) -> None:
    """Benchmark cost AND pass-rate of model@effort variants against the eval suite.

    Runs the scenario suite once per variant on the metered in-process
    Agent-SDK runner (``--backend sdk`` semantics; the all-skipped gate is
    always armed) and renders one comparison line per variant: scenarios
    passed/executed, pass-rate, total metered cost, mean cost per scenario,
    and cost per pass. A failing scenario is the measurement, not an error —
    the command exits non-zero only when the run itself is broken (nothing
    executed, unknown variant/scenario). Pass-rate noise shrinks with
    ``--trials k`` (each cell's score becomes a k-trial pass-rate).
    """
    ensure_django()
    require_valid_format(output_format)
    tags = parse_model_tags(models)
    specs = _select_specs(scenarios)
    runner = SdkInProcessRunner(max_turns_override=max_turns, require_executed=True)
    rows = collect_matrix_rows(specs, tags, runner=runner, trials=trials, require="any")
    RunGuards.executed(executed=sum(1 for row in rows if not row.skipped), collected=len(rows), required=True)
    if persist:
        persist_matrix_run(rows, models=tags, max_turns=max_turns, baseline=False)
    summaries = summarize_benchmark(rows, tags)
    renderer = render_benchmark_json if output_format == "json" else render_benchmark_text
    typer.echo(renderer(summaries))


def _select_specs(scenarios: str | None) -> list[EvalSpec]:
    """Resolve ``--scenarios`` against the discovered suite, or exit 2 on an unknown name."""
    specs = discover_specs()
    if scenarios is None:
        return specs
    by_name = {spec.name: spec for spec in specs}
    names = [name.strip() for name in scenarios.split(",") if name.strip()]
    unknown = [name for name in names if name not in by_name]
    if unknown:
        typer.echo(f"unknown scenario(s): {', '.join(unknown)}", err=True)
        available = ", ".join(sorted(by_name)) or "(none)"
        typer.echo(f"available scenarios: {available}", err=True)
        raise typer.Exit(code=2)
    return [by_name[name] for name in names]
