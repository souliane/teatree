"""``t3 eval`` — behavioral eval harness commands."""

from pathlib import Path

import typer
from rich.console import Console

from teatree.cli._format_opts import VALID_FORMATS, require_valid_format
from teatree.cli.eval._registration import register_imported_commands
from teatree.cli.eval.all import build_scenarios_table
from teatree.cli.eval.app_helpers import (
    RunReportPaths,
    reject_unsupported_run_output,
    require_api_backend_for_fresh_run,
    require_effort,
    require_spec,
    resolve_benchmark_selection,
    resolve_escalation,
)
from teatree.cli.eval.full_suite_command import register_full_suite_callback
from teatree.cli.eval.lane_filter import filter_specs_by_lane
from teatree.cli.eval.metered_routing import warn_local_metered
from teatree.cli.eval.run_dispatch import ResolvedRun, dispatch_resolved_run
from teatree.cli.eval.run_docker import RunDockerArgs, route_to_docker_if_needed
from teatree.cli.eval.run_modes import DEFAULT_COST_REGRESSION_TOLERANCE, make_grader, require_persist_for_history_gates
from teatree.eval.api_runner import resolve_max_turns_override, resolve_metered_budget_usd, resolve_metered_effort
from teatree.eval.backends import API_BACKEND, FRESH_CLAUDE_BACKENDS, TRANSCRIPT_BACKEND
from teatree.eval.discovery import discover_specs
from teatree.eval.lane_shard import ShardSpecError, filter_specs_by_shard
from teatree.eval.model_variant import EFFORT_LEVELS
from teatree.eval.parallel import DEFAULT_PARALLEL
from teatree.utils.django_bootstrap import ensure_django

_RUN_FORMATS = (*VALID_FORMATS, "html")

#: The metered ``t3 eval run --backend api`` lane's GENEROUS, configurable
#: defaults — the cheap 0.10 floor truncated finishing scenarios (a truncated run
#: measures the cap, not behaviour), and the model's DEFAULT effort understates
#: real high-effort usage. Resolved once here (env-overridable) so the CLI default
#: is generous + representative; a per-invocation flag still overrides.
METERED_DEFAULT_BUDGET_USD = resolve_metered_budget_usd()
METERED_DEFAULT_EFFORT = resolve_metered_effort()

eval_app = typer.Typer(
    no_args_is_help=False,
    help="Behavioral eval harness — bare `t3 eval` runs the whole suite; subcommands target one lane.",
)
register_imported_commands(eval_app)


@eval_app.command("list")
def list_scenarios() -> None:
    """List discovered eval scenarios as a table (Name, Scenario, Agent, File, Asserts)."""
    ensure_django()
    specs = discover_specs()
    if not specs:
        typer.echo("(no scenarios discovered)")
        return
    Console().print(build_scenarios_table(specs))


@eval_app.command("run")
# ast-grep-ignore: ac-django-no-complexity-suppressions
def run(  # noqa: PLR0913, PLR0917 — typer command: each param maps 1:1 to a public ``t3 eval run`` flag. The arg list IS the CLI contract.
    name: str | None = typer.Argument(None, help="Scenario name to run (omit to run all)."),
    lane: str | None = typer.Option(
        None,
        "--lane",
        help=(
            "Run only scenarios in this lane (clean_room | under_load). Omit to run every lane "
            "(default, unchanged). The cheap PR-path gate and the weekly metered lane read the "
            "same catalog but pass different --lane subsets."
        ),
    ),
    shard: str | None = typer.Option(
        None,
        "--shard",
        help=(
            "Run only the index/total shard of the (lane-filtered) catalog, e.g. '2/6'. A "
            "deterministic partition by scenario name — every scenario in exactly one shard, none "
            "dropped or duplicated. The weekly metered lane shards each lane into budget-safe legs "
            "on a lane-aware ceiling (clean_room ~182 into ~13, under_load 14 into 4 — its "
            "roster-spawning scenarios are far slower); omit (default) to run the whole lane unchanged."
        ),
    ),
    output_format: str = typer.Option(
        "text", "--format", help="Report format: text, json, or html (single-trial; html is a self-contained file)."
    ),
    max_turns: int | None = typer.Option(
        None,
        "--max-turns",
        help=(
            "Override the scenario's max_turns. Omitted, it reads the T3_EVAL_MAX_TURNS global knob "
            "(an escape hatch), else defers to each scenario's own max_turns — the per-scenario turn "
            "budget, mirroring per-scenario cost. The metered lane's USD budget is the real safety net."
        ),
    ),
    max_budget_usd: float = typer.Option(
        METERED_DEFAULT_BUDGET_USD,
        "--max-budget-usd",
        help=(
            "Per-run USD budget circuit breaker for the metered api runner. Defaults GENEROUS "
            "(env-configurable via T3_EVAL_MAX_BUDGET_USD) so a finishing scenario COMPLETES "
            "rather than truncating — a truncated run measures the cap, not behaviour. Raise it "
            "for a costly --models/--trials run. An over-budget scenario is recorded as a "
            "budget_exceeded FAIL, not a crash."
        ),
    ),
    effort: str = typer.Option(
        METERED_DEFAULT_EFFORT,
        "--effort",
        help=(
            "Representative reasoning effort for the metered api lane "
            f"({', '.join(EFFORT_LEVELS)}; default '{METERED_DEFAULT_EFFORT}', env-configurable via "
            "T3_EVAL_EFFORT). The lane otherwise runs at the model's DEFAULT effort while real "
            "usage is high — so a default-effort pass-rate is pessimistic. A scenario's own "
            "model@effort still wins over this lane default."
        ),
    ),
    trials: int = typer.Option(1, "--trials", help="Re-run each scenario this many times (pass@k)."),
    require: str = typer.Option(
        "any",
        "--require",
        help="With --trials > 1: 'any' (pass@k) or 'all' (pass^k regression gate).",
    ),
    models: str | None = typer.Option(
        None,
        "--models",
        help=(
            "Comma-separated model matrix (e.g. opus,sonnet,haiku); runs the suite once per model. "
            "Each entry may carry a reasoning-effort variant as model@effort (e.g. "
            "claude-opus-4-8@xhigh) — the tag is the column/ledger identity."
        ),
    ),
    persist: bool = typer.Option(  # noqa: FBT001 — typer boolean flag, not a positional bool foot-gun.
        True,
        "--persist/--no-persist",
        help="Persist this run into the run-history ledger (read back via `t3 eval history`).",
    ),
    baseline: bool = typer.Option(  # noqa: FBT001 — typer boolean flag, not a positional bool foot-gun.
        False,
        "--baseline",
        help="Mark the persisted run as the baseline for its model.",
    ),
    gate_regressions: bool = typer.Option(  # noqa: FBT001 — typer boolean flag, not a positional bool foot-gun.
        False,
        "--gate-regressions",
        help="Diff this run against each model's current baseline; any regression exits non-zero.",
    ),
    gate_cost_regression: bool = typer.Option(  # noqa: FBT001 — typer boolean flag, not a positional bool foot-gun.
        False,
        "--gate-cost-regression",
        help=(
            "Diff this run's per-scenario cost against each model's baseline cost; a relative "
            "rise beyond --cost-regression-tolerance exits non-zero. Distinct from an absolute "
            "ceiling: a zero-cost (subscription/free) baseline is skipped, never divided by."
        ),
    ),
    cost_regression_tolerance: float = typer.Option(
        DEFAULT_COST_REGRESSION_TOLERANCE,
        "--cost-regression-tolerance",
        help=(
            "Relative per-scenario cost rise --gate-cost-regression tolerates before failing "
            "(default 0.20 = +20% vs the baseline). A scenario rising more than this exits non-zero."
        ),
    ),
    gate_cost_bounds: bool = typer.Option(  # noqa: FBT001 — typer boolean flag, not a positional bool foot-gun.
        False,
        "--gate-cost-bounds",
        help=(
            "Check this run's per-scenario cost against the CHECKED-IN ceilings in "
            "evals/cost_bounds.yaml (calibrated bound x (1 + margin)). A scenario over its "
            "ceiling — OR a configured scenario the run recorded no cost for (fail-loud, never "
            "skip-as-pass) — exits non-zero. The absolute-ceiling counterpart of "
            "--gate-cost-regression (relative drift vs the mutable DB baseline)."
        ),
    ),
    judge: bool = typer.Option(  # noqa: FBT001 — typer boolean flag, not a positional bool foot-gun.
        False,
        "--judge/--no-judge",
        help="Grade scenarios that opt in (a `judge:` block) with an LLM judge in addition to matchers.",
    ),
    judge_budget: int = typer.Option(
        20,
        "--judge-budget",
        help="Max number of LLM-judge calls per run (cost cap).",
    ),
    backend: str = typer.Option(
        TRANSCRIPT_BACKEND,
        "--backend",
        help=(
            "Execution backend for a single-trial run: 'transcript' (default — REUSE an "
            "already-recorded run by grading its on-disk transcript, $0 extra; see "
            "`t3 eval prepare-transcript`) or 'api' (RUN the model fresh in-process via the "
            "Agent SDK, on the credential the eval_credential knob selects — default "
            "subscription OAuth (#2707 reversal), or the metered API key; runs in-container "
            "by default or directly on the host with --local) or 'anthropic_api' (RUN the "
            "same Claude model fresh through the Anthropic Messages API DIRECTLY, no `claude` "
            "CLI child — the CLI-free lane for a harness that forbids the Claude Code CLI, "
            "metered on ANTHROPIC_API_KEY) or 'pydantic_ai' (RUN a non-Claude model through "
            "the provider-agnostic harness seam, OrcaRouter BYOK — the model-evolution lane). "
            "--trials and --models require --backend api."
        ),
    ),
    transcript_dir: Path | None = typer.Option(
        None,
        "--transcript-dir",
        help="Directory of <scenario>.jsonl transcripts for the 'transcript' backend (default: cwd).",
    ),
    require_executed: bool = typer.Option(  # noqa: FBT001 — typer boolean flag, not a positional bool foot-gun.
        False,
        "--require-executed",
        help=(
            "Fail when the suite collected scenarios but executed none (all skipped). "
            "AUTO-ON for the api backend and --trials/--models (a fresh-run lane that "
            "executes nothing always fails loud); the flag only matters for the "
            "transcript backend, whose pre-transcript all-skip is legitimate."
        ),
    ),
    docker: bool = typer.Option(  # noqa: FBT001 — typer boolean flag, not a positional bool foot-gun.
        False,
        "--docker",
        help=(
            "Force running inside the CI image (dev/Dockerfile.test) for ANY backend. The api "
            "lane ALREADY defaults to the container; this forces it for the transcript lane too."
        ),
    ),
    local: bool = typer.Option(  # noqa: FBT001 — typer boolean flag, not a positional bool foot-gun.
        False,
        "--local",
        help=(
            "Run the fresh api lane directly on the host instead of Docker. Use for durable-history "
            "gates that must persist/read the runner DB; otherwise Docker remains the reproducible path."
        ),
    ),
    parallel: int = typer.Option(
        DEFAULT_PARALLEL,
        "--parallel",
        help=(
            "Run this many scenarios concurrently (each SDK scenario run is I/O-bound; a bounded "
            "pool cuts wall-clock from Nxlatency to ~latency). Default 1 = sequential."
        ),
    ),
    transcript_html: Path | None = typer.Option(
        None,
        "--transcript-html",
        help=(
            "Write a self-contained per-trial TRANSCRIPT report (each scenario's per-trial "
            "PASS/FAIL plus the agent's reasoning + tool calls) to this path — the durable, "
            "uploadable artifact a maintainer reads to diagnose a red lane. Produced from THIS "
            "run's results (no suite re-run, no ledger), so it survives the --no-persist "
            "ephemeral-container CI path. Supported on a --trials run (the metered CI shape)."
        ),
    ),
    summary_md: Path | None = typer.Option(
        None,
        "--summary-md",
        help=(
            "Write a SANITIZED aggregate markdown dashboard (overall counts + total cost + "
            "model + a `scenario | lane | verdict | trials | cost` table) to this path. Unlike "
            "--transcript-html it carries NO transcript (no reasoning, tool calls, or judge "
            "rationale), so it is the PUBLISH-safe artifact for a PR's $GITHUB_STEP_SUMMARY and "
            "the weekly public dashboard. Written from THIS run's results (single-trial AND --trials)."
        ),
    ),
    summary_json: Path | None = typer.Option(
        None,
        "--summary-json",
        help=(
            "Write a PUBLISH-safe per-scenario JSON (generated_at, model, head_sha, totals, and a "
            "scenarios[] of name/lane/verdict + the triage discriminators + a triage_class) to this "
            "path. Like --summary-md it carries NO transcript, so it is safe to upload; unlike it, it "
            "is machine-readable — the CI heal loop's eval-heal-<sha> artifact. Written from THIS run's "
            "results (single-trial AND --trials)."
        ),
    ),
    benchmark: bool = typer.Option(  # noqa: FBT001 — typer boolean flag, not a positional bool foot-gun.
        False,
        "--benchmark",
        help=(
            "Run every (filtered) scenario against ALL three tier models "
            "(frontier, balanced, cheap — resolved through the single TIER_MODELS "
            "constant) and render a comparison matrix + a self-contained HTML "
            "dashboard. The canonical CI benchmark entry — adopting a new model "
            "needs no flag edit. Routes through the metered matrix lane (--backend api)."
        ),
    ),
    model: str | None = typer.Option(
        None,
        "--model",
        help=(
            "Force the WHOLE suite onto one model[@effort], overriding every "
            "scenario's tier/phase. A single-trial metered run against that one "
            "model — e.g. spot-check the suite on a candidate model. Mutually "
            "exclusive with --benchmark/--models."
        ),
    ),
    escalate_on_fail: bool = typer.Option(  # noqa: FBT001 — typer boolean flag, not a positional bool foot-gun.
        False,
        "--escalate-on-fail",
        help=(
            "ADAPTIVE escalation for the cheap single-trial PR lane: a scenario that FAILS the "
            "single trial is not yet a hard red — it is re-run at --escalate-trials higher trials. "
            "The lane reds only on a CONFIRMED failure (every escalation trial also failed); a "
            "scenario that recovers on any escalation trial is reported flaky-but-passing, not red. "
            "Single-trial only (rejects --trials>1/--models, which already aggregate)."
        ),
    ),
    escalate_trials: int = typer.Option(
        3,
        "--escalate-trials",
        help=(
            "How many trials a --escalate-on-fail re-run uses to confirm a single-trial failure "
            "(default 3). Must be >= 2 — one trial is no escalation. Only the scenarios that "
            "failed the single trial are re-run, so the spend is bounded by the failures, not the "
            "whole changed set."
        ),
    ),
) -> None:
    """Run one scenario by name, or all scenarios when no name is given.

    With ``--trials k`` each scenario runs ``k`` times and the verdict is
    aggregated by ``--require`` (``any`` = pass@k, ``all`` = pass^k). ``--models``
    runs the suite once per model and renders a comparison matrix. A single trial
    against the default backend is the legacy behavior.

    Each run is recorded into the run-history ledger (``t3 eval history``) unless
    ``--no-persist`` is given. ``--baseline`` marks the persisted run as the
    baseline for its model — the reference ``--gate-regressions`` compares a
    later candidate run against (a regression exits non-zero).

    ``--backend transcript`` (default) REUSES an already-recorded run by grading
    its on-disk transcript — ``$0`` extra, no model run (produce the transcripts
    in-session via ``t3 eval prepare-transcript`` first for the prompts + expected
    paths). ``--backend api`` RUNS the model fresh in-process via the Agent SDK
    (which spawns the ``claude`` CLI as its child), on the credential the
    ``eval_credential`` knob selects — default subscription OAuth (#2707 reversal),
    or the metered API key; CI passes ``--backend api`` explicitly via the
    standalone ``eval.yml`` job. ``--trials``/``--models`` require the fresh-run
    ``api`` runner and reject the transcript backend.

    ``--require-executed`` fails the run when the suite collected scenarios but
    executed none (every scenario skipped — typically ``claude`` not on PATH /
    not authenticated), so a decorative all-skipped run cannot pass green. CI
    arms it always; local runs leave it off so the transcript backend's
    legitimate pre-transcript all-skip stays green.

    ``--docker`` runs the suite inside the CI image. The fresh-run ``api`` lane is
    meant to run in-container, never on the host — the runner forwards the host's
    SELECTED eval credential var in via docker's ``-e VARNAME`` pass-through, so it
    authenticates the SDK's ``claude`` child inside a clean container and never
    lands on the command line (only the selected credential is forwarded; the
    conflicting one is stripped by the isolation env).

    ``--local`` is the explicit host escape for durable-history gates that must
    persist/read the runner DB, or for a quick host check.

    ``--parallel N`` runs N scenarios concurrently (each SDK scenario run is
    I/O-bound, so a bounded worker pool cuts the suite's wall-clock from
    Nxlatency toward ~latency). Default 1 = today's sequential behaviour.
    """
    # The fresh-run api lane (and the always-fresh-run --trials/--models lanes)
    # defaults to running IN the CI container — the reproducible gate must never
    # accidentally run on the host. --docker forces the container for any backend;
    # the T3_EVAL_IN_CONTAINER marker the docker runner sets keeps the in-container
    # re-invocation in-process (no re-route loop).
    effort_level = require_effort(effort)
    max_turns = resolve_max_turns_override(max_turns)
    # --benchmark expands to the three tier models (matrix lane + HTML); --model
    # forces the whole suite onto one model. At most one of benchmark/model/models
    # may be set. The benchmark HTML dashboard is written to --transcript-html.
    selection = resolve_benchmark_selection(benchmark=benchmark, model=model, models=models, html_out=transcript_html)
    models = selection.models
    # --benchmark (the 3-tier matrix) and --model (force one model) both run a
    # fresh metered pass, so the metered api backend is implied — a transcript
    # grade of a freshly-forced model is nonsensical. Mirror how --models always
    # drives the api matrix lane.
    if benchmark or selection.model_override is not None:
        backend = API_BACKEND
    metered = (
        backend in FRESH_CLAUDE_BACKENDS or trials > 1 or models is not None or selection.model_override is not None
    )
    require_api_backend_for_fresh_run(backend=backend, trials=trials, models=models)
    escalation = resolve_escalation(
        escalate_on_fail=escalate_on_fail, escalate_trials=escalate_trials, trials=trials, models=models
    )
    require_persist_for_history_gates(
        persist=persist,
        baseline=baseline,
        gate_regressions=gate_regressions,
        gate_cost_regression=gate_cost_regression,
        gate_cost_bounds=gate_cost_bounds,
    )
    route_to_docker_if_needed(
        RunDockerArgs(
            name=name,
            lane=lane,
            shard=shard,
            output_format=output_format,
            max_turns=max_turns,
            max_budget_usd=max_budget_usd,
            effort=effort_level,
            trials=trials,
            require=require,
            # The docker re-invocation re-derives the lane from the ORIGINAL flags
            # (--benchmark/--model), not the resolved tier list, so it re-runs the
            # same expansion in-container.
            models=models if not benchmark else None,
            backend=backend,
            require_executed=require_executed,
            parallel=parallel,
            transcript_html=transcript_html,
            summary_md=summary_md,
            summary_json=summary_json,
            benchmark=benchmark,
            model=model,
            escalate_on_fail=escalate_on_fail,
            escalate_trials=escalate_trials,
        ),
        docker=docker,
        local=local,
        metered=metered,
        baseline=baseline,
        gate_regressions=gate_regressions,
        gate_cost_regression=gate_cost_regression,
        gate_cost_bounds=gate_cost_bounds,
    )
    if local:
        warn_local_metered(metered=metered)
    ensure_django()
    require_valid_format(output_format, _RUN_FORMATS)
    reject_unsupported_run_output(
        output_format=output_format,
        # Under --benchmark, --transcript-html is REPURPOSED as the matrix HTML
        # dashboard out (not a per-trial transcript), so it is permitted alongside
        # the resolved tier models. Pass None to the guard to skip that rejection.
        reports=RunReportPaths(
            transcript_html=None if benchmark else transcript_html,
            summary_md=summary_md,
            summary_json=summary_json,
        ),
        trials=trials,
        models=None if benchmark else models,
    )
    if name is None:
        specs = filter_specs_by_lane(discover_specs(), lane)
        try:
            specs = filter_specs_by_shard(specs, shard)
        except ShardSpecError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from None
    else:
        specs = [require_spec(name)]
    grader = make_grader(enabled=judge, judge_budget=judge_budget)
    # "If we run the fresh-run lane, of course we want it executed." Both fresh
    # Claude backends (api and the CLI-free anthropic_api — and the always-fresh-run
    # --trials/--models lanes) arm the all-skipped gate unconditionally: a fresh run
    # that executes nothing must fail loud, never pass. --require-executed stays only
    # as the opt-in knob for the transcript backend's legitimate pre-transcript all-skip.
    api_metered = (
        backend in FRESH_CLAUDE_BACKENDS or trials > 1 or models is not None or selection.model_override is not None
    )
    require_executed = require_executed or api_metered
    dispatch_resolved_run(
        specs,
        ResolvedRun(
            backend=backend,
            max_turns=max_turns,
            transcript_dir=transcript_dir,
            require_executed=require_executed,
            max_budget_usd=max_budget_usd,
            effort=effort_level,
            parallel=parallel,
            output_format=output_format,
            judge=judge,
            # Under --benchmark, --transcript-html is the matrix HTML out (handled
            # by benchmark_html), not a single-trial transcript — so suppress it here.
            transcript_html=None if benchmark else transcript_html,
            summary_md=summary_md,
            summary_json=summary_json,
            trials=trials,
            require=require,
            models=models,
            persist=persist,
            baseline=baseline,
            gate_regressions=gate_regressions,
            gate_cost_regression=gate_cost_regression,
            cost_regression_tolerance=cost_regression_tolerance,
            gate_cost_bounds=gate_cost_bounds,
            model_override=selection.model_override,
            benchmark_html=selection.benchmark_html,
        ),
        grader=grader,
        escalation=escalation,
    )


register_full_suite_callback(eval_app)
