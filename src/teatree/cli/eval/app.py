"""``t3 eval`` — behavioral eval harness commands."""

import json
import sys
from pathlib import Path

import typer
from rich.console import Console

from teatree.cli._format_opts import VALID_FORMATS, require_valid_format
from teatree.cli.eval.all import (
    build_scenarios_table,
    build_summary_table,
    coverage_lane,
    hint_missing_transcripts,
    negative_control_lane,
    regression_lane,
    run_ai_lane,
    transcript_replay_lane,
    trigger_lane,
)
from teatree.cli.eval.capture_subagent import capture_subagent
from teatree.cli.eval.docker import DockerUnavailableError, run_eval_in_docker
from teatree.cli.eval.multi_trial import run_model_matrix_lane, run_pass_at_k_lane
from teatree.cli.eval.negative_control import negative_control
from teatree.cli.eval.run_modes import (
    build_subscription_manifest,
    gate_run_regressions,
    guard_executed,
    make_grader,
    persist_single,
    render_subscription_text,
)
from teatree.cli.eval.transcript_replay import replay_transcript_for_all, transcript_replay
from teatree.eval.backends import SUBSCRIPTION_BACKEND, SubscriptionTranscriptRunner, UnknownBackendError, make_runner
from teatree.eval.coverage import render_json as render_coverage_json
from teatree.eval.coverage import render_text as render_coverage_text
from teatree.eval.coverage import skill_eval_coverage
from teatree.eval.discovery import discover_specs, find_spec
from teatree.eval.models import EvalSpec
from teatree.eval.negative_control import run_negative_control
from teatree.eval.regression_corpus import render_json as render_regression_json
from teatree.eval.regression_corpus import render_text as render_regression_text
from teatree.eval.regression_corpus import run_regression_corpus
from teatree.eval.report import evaluate, render_html, render_json, render_text
from teatree.eval.trigger_qa import render_json as render_trigger_json
from teatree.eval.trigger_qa import render_text as render_trigger_text
from teatree.eval.trigger_qa import run_trigger_qa
from teatree.utils.django_bootstrap import ensure_django

_RUN_FORMATS = (*VALID_FORMATS, "html")

eval_app = typer.Typer(no_args_is_help=True, help="Behavioral eval harness.")
eval_app.command("negative-control")(negative_control)
eval_app.command("capture-subagent")(capture_subagent)
eval_app.command("transcript-replay")(transcript_replay)


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
def run(  # noqa: PLR0913, PLR0917 — typer command: each param maps 1:1 to a public ``t3 eval run`` flag. The arg list IS the CLI contract.
    name: str | None = typer.Argument(None, help="Scenario name to run (omit to run all)."),
    output_format: str = typer.Option(
        "text", "--format", help="Report format: text, json, or html (single-trial; html is a self-contained file)."
    ),
    max_turns: int | None = typer.Option(
        None,
        "--max-turns",
        help="Override the scenario's max_turns (per-invocation).",
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
        help="Comma-separated model matrix (e.g. opus,sonnet,haiku); runs the suite once per model.",
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
        SUBSCRIPTION_BACKEND,
        "--backend",
        help=(
            "Execution backend for a single-trial run: 'subscription' (default — grade "
            "subscription-produced transcripts, no API spend; see `t3 eval prepare-subscription`) "
            "or 'sdk' (metered claude -p, reserved for CI with ANTHROPIC_API_KEY). --trials and "
            "--models always use the metered sdk runner regardless of this flag."
        ),
    ),
    transcript_dir: Path | None = typer.Option(
        None,
        "--transcript-dir",
        help="Directory of <scenario>.jsonl transcripts for the 'subscription' backend (default: cwd).",
    ),
    require_executed: bool = typer.Option(  # noqa: FBT001 — typer boolean flag, not a positional bool foot-gun.
        False,
        "--require-executed",
        help=(
            "Fail when the suite collected scenarios but executed none (all skipped) — "
            "the CI gate so a decorative run with no claude/ANTHROPIC_API_KEY can't pass green."
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

    ``--backend subscription`` (default) grades transcripts produced on the
    subscription via an in-session sub-agent — no API spend (run
    ``t3 eval prepare-subscription`` first for the prompts + expected paths).
    ``--backend sdk`` shells the metered ``claude -p`` runner — the CI job's path
    (``ANTHROPIC_API_KEY``); CI passes it explicitly. ``--trials``/``--models``
    always use the metered ``sdk`` runner regardless of ``--backend``.

    ``--require-executed`` fails the run when the suite collected scenarios but
    executed none (every scenario skipped — typically ``claude`` not on PATH /
    no ``ANTHROPIC_API_KEY``), so a decorative all-skipped run cannot pass green.
    CI arms it only when a key is configured; local runs leave it off so the
    subscription backend's legitimate pre-transcript all-skip stays green.
    """
    ensure_django()
    require_valid_format(output_format, _RUN_FORMATS)
    if output_format == "html" and (trials > 1 or models is not None):
        typer.echo("--format html is only supported for a single-trial run (not --trials/--models)", err=True)
        raise typer.Exit(code=2)
    specs = discover_specs() if name is None else [_require_spec(name)]
    grader = make_grader(enabled=judge, judge_budget=judge_budget)
    if (trials > 1 or models is not None) and backend == SUBSCRIPTION_BACKEND:
        typer.echo(
            "note: --trials/--models force the metered sdk runner (claude -p, API-billed); "
            "the 'subscription' default does not apply to multi-trial / matrix runs",
            err=True,
        )
    if models is not None:
        run_model_matrix_lane(
            specs,
            models=models,
            max_turns=max_turns,
            trials=trials,
            require=require,
            output_format=output_format,
            persist=persist,
            baseline=baseline,
            gate_regressions=gate_regressions,
            grader=grader,
            require_executed=require_executed,
        )
        return
    if trials > 1:
        run_pass_at_k_lane(
            specs,
            max_turns=max_turns,
            trials=trials,
            require=require,
            output_format=output_format,
            persist=persist,
            baseline=baseline,
            gate_regressions=gate_regressions,
            grader=grader,
            require_executed=require_executed,
        )
        return
    try:
        runner = make_runner(backend, max_turns_override=max_turns, transcript_dir=transcript_dir)
    except UnknownBackendError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from None
    results = [evaluate(spec, runner.run(spec), judge=grader) for spec in specs]
    renderers = {"json": render_json, "html": render_html}
    typer.echo(renderers.get(output_format, render_text)(results))
    if backend == SUBSCRIPTION_BACKEND and isinstance(runner, SubscriptionTranscriptRunner):
        hint_missing_transcripts(runner, [spec for spec, r in zip(specs, results, strict=True) if r.skipped])
    guard_executed(executed=sum(1 for r in results if not r.skipped), collected=len(specs), required=require_executed)
    regressed = False
    if persist:
        record = persist_single(results, specs=specs, max_turns=max_turns, baseline=baseline)
        regressed = gate_run_regressions(record, enabled=gate_regressions)
    if any(not r.passed for r in results) or regressed:
        sys.exit(1)


@eval_app.command("prepare-subscription")
def prepare_subscription(
    name: str | None = typer.Argument(None, help="Scenario name to prepare (omit to prepare all)."),
    transcript_dir: Path | None = typer.Option(
        None,
        "--transcript-dir",
        help="Where `t3 eval capture-subagent` writes each <scenario>.jsonl transcript (default: cwd).",
    ),
    output_format: str = typer.Option("text", "--format", help="Manifest format: text or json."),
) -> None:
    """Emit the per-scenario prompts for a LOCAL subscription eval run.

    The eval CLI is a plain process with no in-session ``Agent`` tool, so it
    cannot itself drive a subscription-covered turn. This command prints, per
    scenario, the agent definition, prompt, and the transcript path the
    ``subscription`` backend will read. The ``/t3:running-evals`` skill is the
    in-session driver: for each entry it dispatches an ``Agent`` sub-agent on the
    prompt, then runs ``t3 eval capture-subagent <scenario>`` to copy the
    sub-agent's JSONL to that path, and finally grades with
    ``t3 eval run --backend subscription``.
    """
    ensure_django()
    require_valid_format(output_format)
    specs = discover_specs() if name is None else [_require_spec(name)]
    manifest = build_subscription_manifest(specs, transcript_dir or Path.cwd())
    typer.echo(json.dumps(manifest, indent=2) if output_format == "json" else render_subscription_text(manifest))


@eval_app.command("history")
def history(
    limit: int = typer.Option(20, "--limit", help="Maximum number of recent runs to show."),
    model: str | None = typer.Option(None, "--model", help="Filter to one model's runs."),
    output_format: str = typer.Option("text", "--format", help="Report format: text or json."),
    show_baseline: bool = typer.Option(  # noqa: FBT001 — typer boolean flag, not a positional bool foot-gun.
        False,
        "--baseline",
        help="Show only the current baseline run(s) and their per-scenario pass-rate.",
    ),
    mark_baseline: int | None = typer.Option(
        None,
        "--mark-baseline",
        help="Mark the run with this id as the baseline for its model, then show history.",
    ),
) -> None:
    """Show recent eval runs and per-scenario pass-rate over time.

    The data substrate the model-regression diff reads. ``--baseline`` shows the
    current reference run per model; ``--mark-baseline <id>`` promotes a run to
    baseline (demoting the prior baseline for that model).
    """
    ensure_django()
    require_valid_format(output_format)
    from teatree.cli.eval.history import mark_run_baseline, render_history_json, render_history_text  # noqa: PLC0415
    from teatree.core.models import EvalRunRecord  # noqa: PLC0415

    if mark_baseline is not None and not mark_run_baseline(mark_baseline):
        typer.echo(f"unknown run id: {mark_baseline}", err=True)
        raise typer.Exit(code=2)
    runs = EvalRunRecord.objects.all()
    if model is not None:
        runs = runs.for_model(model)
    if show_baseline:
        runs = runs.baselines()
    runs = list(runs[:limit])
    renderer = render_history_json if output_format == "json" else render_history_text
    typer.echo(renderer(runs))


@eval_app.command("trigger-qa")
def trigger_qa(
    output_format: str = typer.Option("text", "--format", help="Report format: text or json."),
) -> None:
    """Validate every skill's trigger keywords against the must-fire/must-not-fire corpus.

    Deterministic and free — no ``claude -p`` invocation. An under-trigger
    (in-scope prompt that does not fire) or over-trigger (control prompt that
    does fire) exits non-zero.
    """
    report = run_trigger_qa()
    typer.echo(render_trigger_json(report) if output_format == "json" else render_trigger_text(report))
    if not report.ok:
        sys.exit(1)


@eval_app.command("coverage")
def coverage(
    output_format: str = typer.Option("text", "--format", help="Report format: text or json."),
    fail_on_gap: bool = typer.Option(  # noqa: FBT001 — typer boolean flag, not a positional bool foot-gun.
        False,
        "--fail-on-gap",
        help="Exit non-zero on any coverage gap (Phase B enforcement); default is warn-first (exit 0).",
    ),
) -> None:
    """Report per-skill behavioral-eval coverage: every skill is covered or eval_exempt.

    A skill is COVERED when >=1 discovered scenario targets its ``SKILL.md``
    (flat catalog OR co-located ``skills/<name>/evals.yaml``), or EXEMPT when its
    frontmatter carries a non-empty ``eval_exempt`` reason. A skill that is
    neither is a GAP. Deterministic and free — no ``claude -p`` invocation.
    Warn-first by default (a gap is reported, exit 0); ``--fail-on-gap`` is the
    Phase-B enforcement that exits non-zero on any gap.
    """
    ensure_django()
    require_valid_format(output_format)
    report = skill_eval_coverage()
    typer.echo(render_coverage_json(report) if output_format == "json" else render_coverage_text(report))
    if fail_on_gap and report.gaps:
        sys.exit(1)


@eval_app.command("regression")
def regression(
    output_format: str = typer.Option("text", "--format", help="Report format: text or json."),
) -> None:
    """Run the deterministic regression corpus over the real gate/checker code paths.

    Layer-1 (deterministic, free, no ``claude`` run): each check calls the real
    function for a recurring failure class (branch-currency §940, the
    bare-reference gate, the substrate-merge and maker≠checker floors, the
    pid-anchored loop lease, the migration-graph leaf count) on a must-block and
    a must-allow input. Any violated invariant exits non-zero.
    """
    ensure_django()
    require_valid_format(output_format)
    report = run_regression_corpus()
    typer.echo(render_regression_json(report) if output_format == "json" else render_regression_text(report))
    if not report.ok:
        sys.exit(1)


@eval_app.command("all")
def all_lanes(
    backend: str = typer.Option(
        SUBSCRIPTION_BACKEND,
        "--backend",
        help=(
            "AI-lane backend: 'subscription' (default — grade in-session transcripts, no API spend) "
            "or 'sdk' (metered claude -p, the explicit CI opt-in with ANTHROPIC_API_KEY)."
        ),
    ),
    transcript_dir: Path | None = typer.Option(
        None,
        "--transcript-dir",
        help="Directory of <scenario>.jsonl subscription transcripts for the AI lane (default: cwd).",
    ),
    free_only: bool = typer.Option(  # noqa: FBT001 — typer boolean flag, not a positional bool foot-gun.
        False,
        "--free-only",
        help="Run only the free deterministic lanes (drop the AI lane) — the fast pre-push gate.",
    ),
    docker: bool = typer.Option(  # noqa: FBT001 — typer boolean flag, not a positional bool foot-gun.
        False,
        "--docker",
        help="Run inside the exact CI image (dev/Dockerfile.test) for parity; host-run is the default.",
    ),
) -> None:
    """Run every eval lane in sequence and render one unified summary table.

    The five free deterministic lanes (trigger-qa, skill-coverage, regression,
    negative-control, transcript-replay) always run; skill-coverage is warn-first
    (reports gaps, never FAILs in Phase A); transcript-replay SKIPs when no real session
    transcript is in scope (a missing run is not a violation). The AI lane grades
    subscription-produced transcripts when present; with none on disk it emits the
    subscription manifest plus the in-session recipe and NEVER silently shells the
    metered ``claude -p`` runner. ``--backend sdk`` is the explicit metered opt-in
    (CI's path). ``--free-only`` drops the AI lane entirely — the deterministic,
    token-free, spec-discovery-free gate the pre-push hook runs. ``--docker`` runs
    the same gate inside the exact CI image for environment parity (host-run is the
    default). A SKIP never fails the run; only a real FAIL exits non-zero.
    """
    if docker:
        passthrough = ["all"]
        if free_only:
            passthrough.append("--free-only")
        if backend != SUBSCRIPTION_BACKEND:
            passthrough += ["--backend", backend]
        try:
            raise typer.Exit(code=run_eval_in_docker(passthrough))
        except DockerUnavailableError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from None
    ensure_django()
    target_dir = transcript_dir or Path.cwd()
    lanes = [
        trigger_lane(run_trigger_qa()),
        coverage_lane(skill_eval_coverage()),
        regression_lane(run_regression_corpus()),
        negative_control_lane(run_negative_control()),
        transcript_replay_lane(replay_transcript_for_all()),
    ]
    if not free_only:
        lanes.append(run_ai_lane(discover_specs(), backend=backend, target_dir=target_dir))
    Console().print(build_summary_table(lanes))
    if any(not lane.passed and not lane.skipped for lane in lanes):
        sys.exit(1)


def _require_spec(name: str) -> EvalSpec:
    spec = find_spec(name)
    if spec is None:
        typer.echo(f"unknown scenario: {name!r}", err=True)
        available = ", ".join(s.name for s in discover_specs()) or "(none)"
        typer.echo(f"available scenarios: {available}", err=True)
        raise typer.Exit(code=2)
    return spec
