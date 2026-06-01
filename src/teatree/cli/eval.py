"""``t3 eval`` — behavioral eval harness commands."""

import dataclasses
import json
import os
import sys
from pathlib import Path

import typer

from teatree.claude_sessions import list_sessions
from teatree.eval.discovery import discover_specs, find_spec
from teatree.eval.judge import ClaudeJudge, JudgeBudget
from teatree.eval.matrix import MatrixRow, render_matrix_json, render_matrix_text
from teatree.eval.models import EvalRun, EvalSpec
from teatree.eval.pass_at_k import PassAtKResult, run_pass_at_k
from teatree.eval.report import JudgeOutcome, ScenarioResult, evaluate, render_json, render_text
from teatree.eval.run_store import ScenarioOutcome, diff_against_baseline, new_run_id, record_run
from teatree.eval.runner import ClaudePRunner
from teatree.eval.session_transcript import parse_session_jsonl
from teatree.eval.transcript_conformance import render_report, render_report_json, replay
from teatree.eval.trigger_qa import run_trigger_qa

eval_app = typer.Typer(no_args_is_help=True, help="Behavioral eval harness.")


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
    """List discovered eval scenarios."""
    _bootstrap_django()
    specs = discover_specs()
    if not specs:
        typer.echo("(no scenarios discovered)")
        return
    for spec in specs:
        typer.echo(f"{spec.name}\t{spec.scenario}")


@eval_app.command("run")
def run(  # noqa: PLR0913, PLR0917 — typer command: every param maps 1:1 to a public `eval run` CLI flag (name/format/max-turns/trials/require/models/record/baseline/judge/judge-budget). The arg list IS the CLI contract.
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
        help="Comma-separated model matrix (e.g. opus,sonnet,haiku); runs the suite per model.",
    ),
    record: bool = typer.Option(  # noqa: FBT001 — typer boolean flag.
        False,
        "--record/--no-record",
        help="Persist this run to the eval run-store (required for history/baseline).",
    ),
    baseline: bool = typer.Option(  # noqa: FBT001 — typer boolean flag.
        False,
        "--baseline/--no-baseline",
        help="After recording, diff this run against each model's preceding run; regressions exit non-zero.",
    ),
    judge: bool = typer.Option(  # noqa: FBT001 — typer boolean flag.
        False,
        "--judge/--no-judge",
        help="Grade scenarios that opt in (a `judge:` block) with an LLM judge in addition to matchers.",
    ),
    judge_budget: int = typer.Option(
        20,
        "--judge-budget",
        help="Max number of LLM-judge calls per run (cost cap).",
    ),
) -> None:
    """Run one scenario by name, or all scenarios when no name is given.

    With ``--trials k`` each scenario runs ``k`` times and the verdict is
    aggregated by ``--require`` (``any`` = pass@k, ``all`` = pass^k). A single
    trial (the default) is the legacy behavior. ``--models`` runs the suite once
    per model and renders a comparison matrix. ``--record`` persists the verdicts
    and ``--baseline`` flags scenarios that regressed versus the recorded
    history.
    """
    _bootstrap_django()
    specs = discover_specs() if name is None else [_require_spec(name)]
    if output_format not in {"text", "json"}:
        typer.echo(f"unknown --format {output_format!r}; use 'text' or 'json'", err=True)
        raise typer.Exit(code=2)
    if baseline and not record:
        typer.echo("--baseline requires --record (a run must be persisted to diff it)", err=True)
        raise typer.Exit(code=2)
    grader = _make_grader(enabled=judge, judge_budget=judge_budget)
    if models is not None:
        _run_model_matrix(
            specs,
            models=models,
            max_turns=max_turns,
            trials=trials,
            require=require,
            output_format=output_format,
            record=record,
            baseline=baseline,
            grader=grader,
        )
        return
    if trials > 1:
        _run_pass_at_k(
            specs,
            max_turns=max_turns,
            trials=trials,
            require=require,
            output_format=output_format,
            record=record,
            baseline=baseline,
            grader=grader,
        )
        return
    runner = ClaudePRunner(max_turns_override=max_turns)
    results: list[ScenarioResult] = []
    for spec in specs:
        run_result = runner.run(spec)
        results.append(evaluate(spec, run_result, judge=grader))
    typer.echo(render_json(results) if output_format == "json" else render_text(results))
    regressed = False
    if record:
        run_id = record_run([_outcome_from_result(r) for r in results])
        regressed = _maybe_report_baseline(run_id, enabled=baseline)
    if any(not r.passed for r in results) or regressed:
        sys.exit(1)


def _run_pass_at_k(  # noqa: PLR0913 — each kwarg threads one `eval run` CLI flag through the pass@k path.
    specs: list[EvalSpec],
    *,
    max_turns: int | None,
    trials: int,
    require: str,
    output_format: str,
    record: bool = False,
    baseline: bool = False,
    model_override: str | None = None,
    grader=None,  # noqa: ANN001 — JudgeGrader | None, kept local to the CLI.
) -> bool:
    """Run the pass@k path; return ``True`` when any scenario failed or regressed."""
    if require not in {"any", "all"}:
        typer.echo(f"unknown --require {require!r}; use 'any' or 'all'", err=True)
        raise typer.Exit(code=2)
    runner = ClaudePRunner(max_turns_override=max_turns)

    def _trial(spec: EvalSpec) -> ScenarioResult:
        return evaluate(spec, runner.run(spec), judge=grader)

    effective_specs = [_with_model(spec, model_override) for spec in specs] if model_override else specs
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
    regressed = False
    if record:
        model_name = model_override or (effective_specs[0].model if effective_specs else "")
        outcomes = [_outcome_from_pass_at_k(r, model_name) for r in results]
        run_id = record_run(outcomes)
        regressed = _maybe_report_baseline(run_id, enabled=baseline)
    failed = any(not r.ok for r in results) or regressed
    if failed and model_override is None:
        sys.exit(1)
    return failed


def _with_model(spec: EvalSpec, model: str) -> EvalSpec:
    return dataclasses.replace(spec, model=model)


def _make_grader(*, enabled: bool, judge_budget: int):  # noqa: ANN202 — returns JudgeGrader | None, kept local.
    """Return an LLM-judge grader closure when ``--judge`` is set, else ``None``."""
    if not enabled:
        return None
    claude_judge = ClaudeJudge(budget=JudgeBudget(max_calls=judge_budget))

    def _grade(spec: EvalSpec, run: EvalRun) -> JudgeOutcome:
        verdict = claude_judge.grade(spec, run)
        return JudgeOutcome(passed=verdict.passed, skipped=verdict.skipped, rationale=verdict.rationale)

    return _grade


def _outcome_from_result(result: ScenarioResult) -> ScenarioOutcome:
    return ScenarioOutcome(
        scenario=result.spec.name,
        model=result.spec.model,
        passed=result.passed and not result.skipped,
        score=0.0 if result.skipped else (1.0 if result.passed else 0.0),
        trials=1,
        skipped=result.skipped,
    )


def _outcome_from_pass_at_k(result: PassAtKResult, model: str) -> ScenarioOutcome:
    return ScenarioOutcome(
        scenario=result.spec_name,
        model=model,
        passed=result.ok and not result.skipped,
        score=0.0 if result.skipped else result.pass_rate,
        trials=result.trials,
        skipped=result.skipped,
    )


def _maybe_report_baseline(run_id: str, *, enabled: bool) -> bool:
    """Render the baseline diff for *run_id*; return ``True`` if anything regressed."""
    if not enabled:
        return False
    report = diff_against_baseline(run_id)
    if not report.entries and not report.new_scenarios:
        typer.echo("baseline: no prior runs recorded — nothing to compare")
        return False
    for entry in report.regressions:
        typer.echo(
            f"REGRESSED {entry.scenario} [{entry.model}]: {entry.baseline_score:.2f} -> {entry.current_score:.2f}"
        )
    for entry in report.improvements:
        typer.echo(
            f"IMPROVED {entry.scenario} [{entry.model}]: {entry.baseline_score:.2f} -> {entry.current_score:.2f}"
        )
    for scenario in report.new_scenarios:
        typer.echo(f"NEW {scenario} (no baseline)")
    typer.echo(f"\nbaseline: {len(report.regressions)} regressed, {len(report.improvements)} improved")
    return not report.ok


def _run_model_matrix(  # noqa: PLR0913 — each kwarg threads one `eval run` CLI flag through the matrix path.
    specs: list[EvalSpec],
    *,
    models: str,
    max_turns: int | None,
    trials: int,
    require: str,
    output_format: str,
    record: bool,
    baseline: bool,
    grader=None,  # noqa: ANN001 — JudgeGrader | None, kept local to the CLI.
) -> None:
    """Run the suite once per model and render a per-model comparison."""
    model_list = [m.strip() for m in models.split(",") if m.strip()]
    if not model_list:
        typer.echo("--models was empty; pass e.g. --models opus,sonnet,haiku", err=True)
        raise typer.Exit(code=2)
    runner = ClaudePRunner(max_turns_override=max_turns)
    rows: list[MatrixRow] = []
    run_id = new_run_id() if record else None
    for model in model_list:
        for spec in specs:
            scoped = _with_model(spec, model)
            outcome = _matrix_trial(runner, scoped, trials=trials, require=require, grader=grader)
            rows.append(outcome)
    if output_format == "json":
        typer.echo(render_matrix_json(rows, model_list, specs))
    else:
        typer.echo(render_matrix_text(rows, model_list, specs))
    regressed = False
    if record and run_id is not None:
        outcomes = [
            ScenarioOutcome(
                scenario=row.scenario,
                model=row.model,
                passed=row.passed,
                score=row.score,
                trials=row.trials,
                skipped=row.skipped,
            )
            for row in rows
        ]
        record_run(outcomes, run_id=run_id)
        regressed = _maybe_report_baseline(run_id, enabled=baseline)
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


@eval_app.command("history")
def history(
    model: str | None = typer.Option(None, "--model", help="Filter to runs that touched this model."),
    limit: int = typer.Option(20, "--limit", help="Maximum number of past runs to list."),
    output_format: str = typer.Option("text", "--format", help="Report format: text or json."),
) -> None:
    """List past recorded eval runs (newest first) from the run-store.

    Reads the durable :class:`EvalRunRecord` ledger written by ``t3 eval run
    --record``. Each line is one run: its id, timestamp, the models it touched,
    and the pass/fail/skip tally.
    """
    _bootstrap_django()
    from teatree.core.models import EvalRunRecord  # noqa: PLC0415

    summaries = EvalRunRecord.objects.runs(model=model, limit=limit)
    if output_format == "json":
        typer.echo(
            json.dumps(
                [
                    {
                        "run_id": s.run_id,
                        "recorded_at": s.recorded_at.isoformat(),
                        "models": sorted(s.models),
                        "total": s.total,
                        "passed": s.passed,
                        "failed": s.failed,
                        "skipped": s.skipped,
                    }
                    for s in summaries
                ],
                indent=2,
            )
        )
        return
    if not summaries:
        typer.echo("(no recorded runs)")
        return
    for s in summaries:
        models_str = ",".join(sorted(s.models))
        typer.echo(
            f"{s.run_id[:12]}  {s.recorded_at.isoformat(timespec='seconds')}  "
            f"[{models_str}]  {s.passed} passed, {s.failed} failed, {s.skipped} skipped"
        )


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
    if output_format == "json":
        typer.echo(
            json.dumps(
                {
                    "ok": report.ok,
                    "checks": [
                        {"skill": c.skill, "prompt": c.prompt, "should_fire": c.should_fire, "fired": c.fired}
                        for c in report.checks
                    ],
                },
                indent=2,
            )
        )
    else:
        for check in report.failures:
            kind = "under-trigger (expected fire, none)" if check.should_fire else "over-trigger (fired, unexpected)"
            typer.echo(f"FAIL {check.skill}: {kind}\n  prompt: {check.prompt}")
        passed = len(report.checks) - len(report.failures)
        typer.echo(f"\nsummary: {passed} passed, {len(report.failures)} failed (of {len(report.checks)})")
    if not report.ok:
        sys.exit(1)


def _require_spec(name: str) -> EvalSpec:
    spec = find_spec(name)
    if spec is None:
        typer.echo(f"unknown scenario: {name!r}", err=True)
        available = ", ".join(s.name for s in discover_specs()) or "(none)"
        typer.echo(f"available scenarios: {available}", err=True)
        raise typer.Exit(code=2)
    return spec


def _resolve_transcript(*, latest: bool, session: str | None, file: Path | None) -> Path | None:
    """Resolve which on-disk session JSONL to replay, or ``None`` when none found.

    Scoped to the current project slug (the cwd-derived project directory) so
    the replay never reads another project's logs. ``--file`` wins; then
    ``--session`` looks up a session id within scope; otherwise the most recent
    session for the cwd's project is replayed when ``--latest`` (the default).
    ``--no-latest`` with no ``--session``/``--file`` resolves to nothing.
    """
    if file is not None:
        return file if file.is_file() else None
    if session is not None:
        match = next((s for s in list_sessions(limit=200) if s.session_id == session), None)
    elif latest:
        sessions = list_sessions(limit=200)
        match = sessions[0] if sessions else None
    else:
        match = None
    if match is None:
        return None
    projects_dir = Path.home() / ".claude" / "projects"
    for project_path in projects_dir.iterdir() if projects_dir.is_dir() else []:
        candidate = project_path / f"{match.session_id}.jsonl"
        if candidate.is_file():
            return candidate
    return None


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
    transcript = _resolve_transcript(latest=latest, session=session, file=file)
    if transcript is None:
        typer.echo("SKIP transcript-replay: no session transcript found in scope", err=True)
        return
    events = parse_session_jsonl(transcript.read_text(encoding="utf-8", errors="replace"))
    results = replay(events)
    rendered = render_report_json(results) if output_format == "json" else render_report(results)
    typer.echo(rendered)
    if any(not result.ok for result in results):
        sys.exit(1)
