"""``t3 eval`` — behavioral eval harness commands."""

import json
import os
import sys
from pathlib import Path

import typer

from teatree.claude_sessions import list_sessions
from teatree.eval.discovery import discover_specs, find_spec
from teatree.eval.models import EvalSpec
from teatree.eval.pass_at_k import run_pass_at_k
from teatree.eval.report import ScenarioResult, evaluate, render_json, render_text
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
def run(
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
) -> None:
    """Run one scenario by name, or all scenarios when no name is given.

    With ``--trials k`` each scenario runs ``k`` times and the verdict is
    aggregated by ``--require`` (``any`` = pass@k, ``all`` = pass^k). A single
    trial (the default) is the legacy behavior.
    """
    _bootstrap_django()
    specs = discover_specs() if name is None else [_require_spec(name)]
    if output_format not in {"text", "json"}:
        typer.echo(f"unknown --format {output_format!r}; use 'text' or 'json'", err=True)
        raise typer.Exit(code=2)
    if trials > 1:
        _run_pass_at_k(specs, max_turns=max_turns, trials=trials, require=require, output_format=output_format)
        return
    runner = ClaudePRunner(max_turns_override=max_turns)
    results: list[ScenarioResult] = []
    for spec in specs:
        run_result = runner.run(spec)
        results.append(evaluate(spec, run_result))
    typer.echo(render_json(results) if output_format == "json" else render_text(results))
    if any(not r.passed for r in results):
        sys.exit(1)


def _run_pass_at_k(
    specs: list[EvalSpec],
    *,
    max_turns: int | None,
    trials: int,
    require: str,
    output_format: str,
) -> None:
    if require not in {"any", "all"}:
        typer.echo(f"unknown --require {require!r}; use 'any' or 'all'", err=True)
        raise typer.Exit(code=2)
    runner = ClaudePRunner(max_turns_override=max_turns)

    def _trial(spec: EvalSpec) -> ScenarioResult:
        return evaluate(spec, runner.run(spec))

    results = [run_pass_at_k(spec, _trial, k=trials, require=require) for spec in specs]
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
    if any(not r.ok for r in results):
        sys.exit(1)


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
