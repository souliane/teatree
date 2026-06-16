"""``t3 eval benchmark`` — cost/pass-rate comparison across ``model@effort`` variants.

A thin command over the model-matrix machinery (`teatree.cli.eval.multi_trial`):
it runs the suite once per variant on the metered in-process Agent-SDK runner
(``--backend sdk`` semantics, all-skipped gate always armed), persists the
matrix record, and folds the rows into the per-variant comparison table in
:mod:`teatree.eval.benchmark` — the deliverable for "how does opus@xhigh
compare to fable@medium on pass-rate and cost".

The benchmark is metered, so it defaults to running IN the CI container
(``dev/Dockerfile.test``) — a metered run must never accidentally bill the host.
``--local`` is the explicit host escape (a quick check, NOT the reproducible
gate); the ``T3_EVAL_IN_CONTAINER=1`` marker the docker runner sets makes the
in-container re-invocation run in-process.
"""

import typer

from teatree.cli._format_opts import require_valid_format
from teatree.cli.eval.docker import DockerUnavailableError, run_eval_in_docker
from teatree.cli.eval.metered_routing import should_route_to_docker, warn_local_metered
from teatree.cli.eval.multi_trial import collect_matrix_rows, parse_model_tags
from teatree.cli.eval.run_modes import RunGuards, persist_matrix_run
from teatree.eval.backends import SDK_BACKEND, make_runner
from teatree.eval.benchmark import render_benchmark_json, render_benchmark_text, summarize_benchmark
from teatree.eval.discovery import discover_specs
from teatree.eval.models import EvalSpec
from teatree.utils.django_bootstrap import ensure_django

#: Generous per-run cap for the benchmark lane. The whole point of the benchmark
#: is measuring a model@effort's REAL cost on a scenario, so the cap must be high
#: enough that even an opus-class run at xhigh effort COMPLETES rather than being
#: truncated by the breaker — a truncated run measures the cap, not the model.
#: This is ~20x the cheap-lane default; override per-invocation with
#: ``--max-budget-usd``. (The cheap ``t3 eval run`` lane keeps the 0.10 default.)
BENCHMARK_DEFAULT_BUDGET_USD = 2.0


# ast-grep-ignore: ac-django-no-complexity-suppressions
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
    max_budget_usd: float = typer.Option(
        BENCHMARK_DEFAULT_BUDGET_USD,
        "--max-budget-usd",
        help=(
            "Per-run USD budget circuit breaker (default 2.0 — generous so even an "
            "opus@xhigh scenario COMPLETES rather than truncating; a truncated run "
            "measures the cap, not the model). An over-budget cell is recorded as a "
            "budget_exceeded FAIL, not a crash."
        ),
    ),
    output_format: str = typer.Option("text", "--format", help="Report format: text or json."),
    persist: bool = typer.Option(  # noqa: FBT001 — typer boolean flag, not a positional bool foot-gun.
        True,
        "--persist/--no-persist",
        help="Persist the underlying matrix run into the run-history ledger (`t3 eval history`).",
    ),
    local: bool = typer.Option(  # noqa: FBT001 — typer boolean flag, not a positional bool foot-gun.
        False,
        "--local",
        help=(
            "Run on the HOST instead of the default CI container — a quick local check only. "
            "A host run is NOT the reproducible regression gate (use Docker/CI for that)."
        ),
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

    The benchmark is metered, so it defaults to running in the CI container; pass
    ``--local`` for a quick host check (NOT the reproducible gate). The container
    is ephemeral, so a Docker-routed run is forced ``--no-persist``.
    """
    if should_route_to_docker(metered=True, local=local):
        _dispatch_to_docker(
            models=models,
            scenarios=scenarios,
            trials=trials,
            max_turns=max_turns,
            max_budget_usd=max_budget_usd,
            output_format=output_format,
        )
        return
    if local:
        warn_local_metered(metered=True)
    ensure_django()
    require_valid_format(output_format)
    tags = parse_model_tags(models)
    specs = _select_specs(scenarios)
    runner = make_runner(
        SDK_BACKEND, max_turns_override=max_turns, require_executed=True, max_budget_usd=max_budget_usd
    )
    rows = collect_matrix_rows(specs, tags, runner=runner, trials=trials, require="any")
    RunGuards.executed(executed=sum(1 for row in rows if not row.skipped), collected=len(rows), required=True)
    # The benchmark is always sdk-metered, yet (unlike the single-run lane and the
    # suite) it lacked the unmetered-$0 guard: an executed-but-$0 run (auth ok but
    # billing nothing, or mid-run quota exhaustion) would report a fake-green $0
    # across every variant. Count only genuinely-graded cells (skipped = not
    # provisioned, errored = $0 by construction); if any of those ran yet the lane
    # metered nothing, fail loud like skip_guard.assert_sdk_run_was_metered.
    graded = [row for row in rows if not row.skipped and not row.errored]
    RunGuards.sdk_metered_total(
        backend=SDK_BACKEND,
        executed=len(graded),
        total_cost_usd=sum(row.cost_usd for row in graded),
    )
    if persist:
        persist_matrix_run(rows, models=tags, max_turns=max_turns, baseline=False)
    summaries = summarize_benchmark(rows, tags)
    renderer = render_benchmark_json if output_format == "json" else render_benchmark_text
    typer.echo(renderer(summaries))
    # An errored cell (auth/CLI failure) is counted as executed by the all-skipped
    # gate, so an all-errored run would otherwise exit 0 (fake green). Like the
    # matrix lane (multi_trial.run_model_matrix_lane), surface it as a non-zero
    # exit — a graded FAIL stays a 0-exit datum, an ERRORED run does not.
    if any(row.errored for row in rows):
        raise typer.Exit(code=1)


# ast-grep-ignore: ac-django-no-complexity-suppressions
def _dispatch_to_docker(  # noqa: PLR0913 — each kwarg is one benchmark flag threaded into the container.
    *,
    models: str,
    scenarios: str | None,
    trials: int,
    max_turns: int | None,
    max_budget_usd: float,
    output_format: str,
) -> None:
    """Re-invoke ``t3 eval benchmark`` inside the CI container with the same args.

    The ephemeral container cannot update the durable run-history ledger, so the
    in-container run is forced ``--no-persist``.
    """
    try:
        raise typer.Exit(
            code=run_eval_in_docker(
                _docker_passthrough(
                    models=models,
                    scenarios=scenarios,
                    trials=trials,
                    max_turns=max_turns,
                    max_budget_usd=max_budget_usd,
                    output_format=output_format,
                )
            )
        )
    except DockerUnavailableError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from None


# ast-grep-ignore: ac-django-no-complexity-suppressions
def _docker_passthrough(  # noqa: PLR0913 — each kwarg is one benchmark flag threaded into the container.
    *,
    models: str,
    scenarios: str | None,
    trials: int,
    max_turns: int | None,
    max_budget_usd: float,
    output_format: str,
) -> list[str]:
    """Build the ``benchmark …`` argv re-invoked in the container (forced ``--no-persist``)."""
    args = ["benchmark", "--models", models]
    if scenarios is not None:
        args += ["--scenarios", scenarios]
    if trials != 1:
        args += ["--trials", str(trials)]
    if max_turns is not None:
        args += ["--max-turns", str(max_turns)]
    args += ["--max-budget-usd", str(max_budget_usd)]
    if output_format != "text":
        args += ["--format", output_format]
    args.append("--no-persist")
    return args


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
