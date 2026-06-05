"""``t3 eval`` — behavioral eval harness commands."""

import json
import os
import sys
from pathlib import Path

import typer
from rich.console import Console

from teatree.cli.eval_all import (
    build_scenarios_table,
    build_summary_table,
    hint_missing_transcripts,
    regression_lane,
    run_ai_lane,
    trigger_lane,
)
from teatree.cli.eval_capture_subagent import capture_subagent
from teatree.cli.eval_negative_control import negative_control
from teatree.cli.eval_run_modes import (
    build_subscription_manifest,
    gate_run_regressions,
    guard_executed,
    make_grader,
    persist_matrix_run,
    persist_pass_at_k_run,
    persist_single,
    render_subscription_text,
    with_model,
)
from teatree.cli.eval_transcript_replay import resolve_transcript
from teatree.eval.backends import SUBSCRIPTION_BACKEND, SubscriptionTranscriptRunner, UnknownBackendError, make_runner
from teatree.eval.discovery import discover_specs, find_spec
from teatree.eval.matrix import MatrixRow, render_matrix_json, render_matrix_text
from teatree.eval.models import EvalSpec
from teatree.eval.pass_at_k import run_pass_at_k
from teatree.eval.regression_corpus import render_json as render_regression_json
from teatree.eval.regression_corpus import render_text as render_regression_text
from teatree.eval.regression_corpus import run_regression_corpus
from teatree.eval.report import ScenarioResult, evaluate, render_json, render_text
from teatree.eval.runner import ClaudePRunner
from teatree.eval.session_transcript import parse_session_jsonl
from teatree.eval.transcript_conformance import render_report, render_report_json, replay
from teatree.eval.trigger_qa import render_json as render_trigger_json
from teatree.eval.trigger_qa import render_text as render_trigger_text
from teatree.eval.trigger_qa import run_trigger_qa

eval_app = typer.Typer(no_args_is_help=True, help="Behavioral eval harness.")
eval_app.command("negative-control")(negative_control)
eval_app.command("capture-subagent")(capture_subagent)

_VALID_FORMATS = ("text", "json")


def _require_valid_format(output_format: str) -> None:
    if output_format not in _VALID_FORMATS:
        typer.echo(f"unknown --format {output_format!r}; use 'text' or 'json'", err=True)
        raise typer.Exit(code=2)


def _bootstrap_django() -> None:
    """Ensure Django is configured before overlay discovery runs.

    The overlay loader (``teatree.core.overlay_loader.get_all_overlays``)
    imports modules that touch Django models at import time, which raises
    ``ImproperlyConfigured`` in an unbootstrapped process. ``t3 eval`` is
    one of the few CLI surfaces that may run ahead of any other DB-touching
    command, so we bootstrap explicitly here rather than relying on a
    sibling command having warmed Django for us.
    """
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "teatree.settings")
    import django  # noqa: PLC0415
    from django.apps import apps  # noqa: PLC0415

    if not apps.ready:
        django.setup()


@eval_app.command("list")
def list_scenarios() -> None:
    """List discovered eval scenarios as a table (Name, Scenario, Agent, File, Asserts)."""
    _bootstrap_django()
    specs = discover_specs()
    if not specs:
        typer.echo("(no scenarios discovered)")
        return
    Console().print(build_scenarios_table(specs))


@eval_app.command("run")
def run(  # noqa: PLR0913, PLR0917 — typer command: each param maps 1:1 to a public ``t3 eval run`` flag. The arg list IS the CLI contract.
    name: str | None = typer.Argument(None, help="Scenario name to run (omit to run all)."),
    output_format: str = typer.Option("text", "--format", help="Report format: text or json."),
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
    _bootstrap_django()
    _require_valid_format(output_format)
    specs = discover_specs() if name is None else [_require_spec(name)]
    grader = make_grader(enabled=judge, judge_budget=judge_budget)
    if (trials > 1 or models is not None) and backend == SUBSCRIPTION_BACKEND:
        typer.echo(
            "note: --trials/--models force the metered sdk runner (claude -p, API-billed); "
            "the 'subscription' default does not apply to multi-trial / matrix runs",
            err=True,
        )
    if models is not None:
        _run_model_matrix(
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
        _run_pass_at_k(
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
    typer.echo(render_json(results) if output_format == "json" else render_text(results))
    if backend == SUBSCRIPTION_BACKEND and isinstance(runner, SubscriptionTranscriptRunner):
        hint_missing_transcripts(runner, [spec for spec, r in zip(specs, results, strict=True) if r.skipped])
    guard_executed(executed=sum(1 for r in results if not r.skipped), collected=len(specs), required=require_executed)
    regressed = False
    if persist:
        record = persist_single(results, specs=specs, max_turns=max_turns, baseline=baseline)
        regressed = gate_run_regressions(record, enabled=gate_regressions)
    if any(not r.passed for r in results) or regressed:
        sys.exit(1)


def _run_pass_at_k(  # noqa: PLR0913 — each kwarg threads one `eval run` CLI flag through the pass@k path.
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
    runner = ClaudePRunner(max_turns_override=max_turns)

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
    guard_executed(executed=sum(1 for r in results if not r.skipped), collected=len(specs), required=require_executed)
    regressed = False
    if persist:
        model_name = model_override or (effective_specs[0].model if effective_specs else "")
        record = persist_pass_at_k_run(results, model=model_name, max_turns=max_turns, baseline=baseline)
        regressed = gate_run_regressions(record, enabled=gate_regressions)
    failed = any(not r.ok for r in results) or regressed
    if failed and model_override is None:
        sys.exit(1)
    return failed


def _run_model_matrix(  # noqa: PLR0913 — each kwarg threads one `eval run` CLI flag through the matrix path.
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
    runner = ClaudePRunner(max_turns_override=max_turns)
    rows: list[MatrixRow] = []
    for model in model_list:
        for spec in specs:
            scoped = with_model(spec, model)
            rows.append(_matrix_trial(runner, scoped, trials=trials, require=require, grader=grader))
    if output_format == "json":
        typer.echo(render_matrix_json(rows, model_list, specs))
    else:
        typer.echo(render_matrix_text(rows, model_list, specs))
    guard_executed(executed=sum(1 for row in rows if not row.skipped), collected=len(rows), required=require_executed)
    regressed = False
    if persist:
        record = persist_matrix_run(rows, models=model_list, max_turns=max_turns, baseline=baseline)
        regressed = gate_run_regressions(record, enabled=gate_regressions)
    if any(not row.passed and not row.skipped for row in rows) or regressed:
        sys.exit(1)


def _matrix_trial(
    runner: ClaudePRunner,
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
    _bootstrap_django()
    _require_valid_format(output_format)
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
    _bootstrap_django()
    _require_valid_format(output_format)
    from teatree.cli.eval_history import mark_run_baseline, render_history_json, render_history_text  # noqa: PLC0415
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
    _bootstrap_django()
    _require_valid_format(output_format)
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
) -> None:
    """Run every eval lane in sequence and render one unified summary table.

    Free deterministic lanes (trigger-qa, regression) always run. The AI lane
    grades subscription-produced transcripts when present; with none on disk it
    emits the subscription manifest plus the in-session recipe and NEVER silently
    shells the metered ``claude -p`` runner. ``--backend sdk`` is the explicit
    metered opt-in (CI's path).
    """
    _bootstrap_django()
    target_dir = transcript_dir or Path.cwd()
    lanes = [
        trigger_lane(run_trigger_qa()),
        regression_lane(run_regression_corpus()),
        run_ai_lane(discover_specs(), backend=backend, target_dir=target_dir),
    ]
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


@eval_app.command("transcript-replay")
def transcript_replay(
    latest: bool = typer.Option(True, "--latest/--no-latest", help="Replay the newest session for the cwd's project."),  # noqa: FBT001 — typer boolean flag, not a positional bool foot-gun.
    session: str | None = typer.Option(None, "--session", help="Replay a specific session id (in the cwd's project)."),
    file: Path | None = typer.Option(None, "--file", help="Replay a specific session JSONL file path."),
    output_format: str = typer.Option("text", "--format", help="Report format: text or json."),
) -> None:
    """Replay a real session transcript against teatree behavioural invariants.

    The #169 complement to the #168 gate-liveness corpus: #168 proves the gates
    CAN fire on synthetic payloads; this proves they DID (or weren't needed) in
    a REAL run. Django-free, stdout-only, no transport: privacy by construction.
    Exits non-zero on any invariant violation; skips and exits 0 when no
    transcript is found. The report names only invariant ids and event indexes —
    never a tool input, prompt, hook output, or quote.
    """
    transcript = resolve_transcript(latest=latest, session=session, file=file)
    if transcript is None:
        typer.echo("SKIP transcript-replay: no session transcript found in scope", err=True)
        return
    events = parse_session_jsonl(transcript.read_text(encoding="utf-8", errors="replace"))
    results = replay(events)
    rendered = render_report_json(results) if output_format == "json" else render_report(results)
    typer.echo(rendered)
    if any(not result.ok for result in results):
        sys.exit(1)
