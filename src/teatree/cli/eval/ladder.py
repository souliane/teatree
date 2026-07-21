"""``t3 eval ladder`` — generate the cheapest-green baseline WITHOUT a full matrix.

The full 3-tier matrix (``t3 eval benchmark`` / ``t3 eval run --models``) measures
every model on every scenario to derive ``evals/presets/baseline.yaml``. That is
the right shape for the cost/pass-rate COMPARISON the benchmark exists for, but it
over-pays for the BASELINE question: a scenario haiku already passes never needs
sonnet or opus measured.

This command is the loss-free alternative. It escalates each scenario up the tier
ladder cheapest-first (cheap → balanced → frontier), dispatching a tier only for
the scenarios every cheaper tier FAILED — so opus runs ONLY on the scenarios that
failed both haiku and sonnet. It emits the exact matrix JSON ``t3 eval set-baseline``
already consumes, so the tier-derivation authority is unchanged:

    t3 eval ladder --format json > matrix.json
    t3 eval set-baseline --from matrix.json

The ladder is metered (it RUNS the models), so — like ``t3 eval benchmark`` — it
defaults to running IN the CI container; ``--local`` is the explicit host escape.
Escalation is per-scenario, so a CI shard runs its OWN subset through the ladder
in-process (``--shard i/n``), staying inside one account's usage window with no
cross-pass orchestration and no auto-rotation.
"""

import dataclasses

import typer

from teatree.cli._format_opts import require_valid_format
from teatree.cli.eval.docker import DockerUnavailableError, run_eval_in_docker
from teatree.cli.eval.metered_routing import should_route_to_docker, warn_local_metered
from teatree.cli.eval.run_modes import RunGuards
from teatree.eval.backends import API_BACKEND, ApiRunnerParams, make_runner
from teatree.eval.discovery import discover_specs
from teatree.eval.ladder import LadderPolicy, laddered_tier_models, resolve_ladder_tiers, run_escalation_ladder
from teatree.eval.lane_shard import ShardSpecError, filter_specs_by_shard
from teatree.eval.matrix import MatrixRow, render_matrix_json
from teatree.eval.models import EvalSpec
from teatree.eval.report import ScenarioResult, evaluate
from teatree.utils.django_bootstrap import ensure_django

#: Generous per-run cap, matching the benchmark lane: the whole point of the
#: ladder is measuring a model's REAL pass/fail on a scenario, so the cap must be
#: high enough that even an opus-class cell COMPLETES rather than truncating.
LADDER_DEFAULT_BUDGET_USD = 2.0


@dataclasses.dataclass(frozen=True)
class _LadderFlags:
    """The ladder flags threaded into the in-container re-invocation."""

    shard: str | None
    trials: int
    max_budget_usd: float
    output_format: str


def ladder(
    shard: str | None = typer.Option(
        None,
        "--shard",
        help=(
            "Ladder only the index/total shard of the suite, e.g. '2/6' — a deterministic partition "
            "by scenario name. Each shard escalates its OWN subset in-process, so the sharded "
            "fan-out parallelises across scenarios while staying inside one account's usage window."
        ),
    ),
    trials: int = typer.Option(
        1,
        "--trials",
        help=(
            "Re-run each tier this many times; a tier counts as PASSED only when EVERY trial passed "
            "(require=all), so a flaky scenario escalates rather than being tiered to the cheaper "
            "model. Single-trial results are noisy — raise this to make the baseline decision robust."
        ),
    ),
    max_budget_usd: float = typer.Option(
        LADDER_DEFAULT_BUDGET_USD,
        "--max-budget-usd",
        help=(
            "Per-run USD budget circuit breaker (default 2.0 — generous so a finishing cell "
            "COMPLETES rather than truncating). An over-budget cell is recorded as a "
            "budget_exceeded FAIL, which escalates to the next tier."
        ),
    ),
    output_format: str = typer.Option(
        "text",
        "--format",
        help=(
            "Report format: 'text' (a per-scenario cheapest-tier summary) or "
            "'json' (the matrix payload for set-baseline)."
        ),
    ),
    local: bool = typer.Option(  # noqa: FBT001 — typer boolean flag, not a positional bool foot-gun.
        False,
        "--local",
        help="Run on the HOST instead of the default CI container — a quick local check only.",
    ),
) -> None:
    """Generate the cheapest-green baseline via a tier escalation ladder (no full matrix).

    Escalates each (sharded) scenario cheapest-first and stops at the first tier
    it passes: sonnet runs only on haiku's failures, opus only on the scenarios
    that failed both. ``--format json`` emits the matrix payload
    ``t3 eval set-baseline --from`` consumes; ``--format text`` prints the
    per-scenario cheapest tier plus the scenarios no tier passed (surfaced as a
    genuine failure, never tiered to frontier).

    Metered, so it defaults to the CI container; pass ``--local`` for a host run.
    """
    flags = _LadderFlags(shard=shard, trials=trials, max_budget_usd=max_budget_usd, output_format=output_format)
    if should_route_to_docker(metered=True, local=local):
        _dispatch_to_docker(flags)  # always raises typer.Exit — the container run's exit code.
    if local:
        warn_local_metered(metered=True)
    ensure_django()
    require_valid_format(output_format)
    specs = _select_specs(shard)
    models = laddered_tier_models()
    runner = make_runner(
        API_BACKEND,
        ApiRunnerParams(max_turns_override=None, require_executed=True, max_budget_usd=max_budget_usd),
    )

    def _run_trial(spec: EvalSpec) -> ScenarioResult:
        return evaluate(spec, runner.run(spec))

    rows = run_escalation_ladder(specs, models, run_trial=_run_trial, policy=LadderPolicy(trials=trials, require="all"))
    RunGuards.executed(executed=sum(1 for row in rows if not row.skipped), collected=len(rows), required=True)
    graded = [row for row in rows if not row.skipped]
    RunGuards.api_metered_total(
        backend=API_BACKEND, executed=len(graded), total_cost_usd=sum(row.cost_usd for row in graded)
    )
    if output_format == "json":
        typer.echo(render_matrix_json(rows, models, specs))
    else:
        typer.echo(_render_ladder_text(rows))


def _select_specs(shard: str | None) -> list[EvalSpec]:
    """Resolve ``--shard`` against the discovered suite (whole suite when omitted), or exit 2."""
    try:
        return filter_specs_by_shard(discover_specs(), shard)
    except ShardSpecError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from None


def _render_ladder_text(rows: list[MatrixRow]) -> str:
    """A per-scenario ``<scenario>: <cheapest tier | NO-PASS>`` summary, sorted by name.

    A scenario no tier passed (or every trial skipped) is surfaced with a
    ``NO-PASS`` marker rather than assigned a tier — the ``t3 eval set-baseline``
    step then writes no entry for it, never silently tiering it to frontier.
    """
    tiers = resolve_ladder_tiers(rows)
    lines = ["scenario                         cheapest passing tier", "-" * 54]
    unresolved: list[str] = []
    for name in sorted(tiers):
        model = tiers[name]
        if model is None:
            unresolved.append(name)
            lines.append(f"{name.ljust(32)} NO-PASS (failed/skipped every tier)")
        else:
            lines.append(f"{name.ljust(32)} {model}")
    if unresolved:
        lines.extend(["", f"WARNING: {len(unresolved)} scenario(s) passed no tier: {', '.join(unresolved)}"])
    return "\n".join(lines)


def _dispatch_to_docker(flags: _LadderFlags) -> None:
    """Re-invoke ``t3 eval ladder`` inside the CI container with the same flags."""
    try:
        raise typer.Exit(code=run_eval_in_docker(_docker_passthrough(flags)))
    except DockerUnavailableError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from None


def _docker_passthrough(flags: _LadderFlags) -> list[str]:
    """Build the ``ladder …`` argv re-invoked in the container."""
    args = ["ladder"]
    if flags.shard is not None:
        args += ["--shard", flags.shard]
    if flags.trials != 1:
        args += ["--trials", str(flags.trials)]
    args += ["--max-budget-usd", str(flags.max_budget_usd)]
    if flags.output_format != "text":
        args += ["--format", flags.output_format]
    return args
