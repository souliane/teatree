"""``t3 eval list`` table render + ``t3 eval all`` lane orchestration.

The five free deterministic lanes (skill-triggers, skill-coverage, pinned-regressions,
negative-control, transcript-replay) always run; skill-coverage is warn-first
(reports a gap, never FAILs in Phase A) and transcript-replay surfaces as a SKIP
when no real session transcript is in scope (never a FAIL). The AI/trajectory lane grades
subscription-produced transcripts when they exist on disk; with none it emits the
subscription manifest plus the in-session recipe and NEVER silently shells the
metered ``claude -p`` runner. ``--backend sdk`` is the explicit metered opt-in.
"""

import sys
from collections.abc import Iterable
from itertools import starmap
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from teatree.cli.eval.docker import DockerUnavailableError, run_eval_in_docker
from teatree.cli.eval.run_modes import build_subscription_manifest, render_subscription_text
from teatree.cli.eval.transcript_replay import replay_transcript_for_all
from teatree.cli.eval.verdict import LaneResult, print_verdict
from teatree.eval.backends import SUBSCRIPTION_BACKEND, SubscriptionTranscriptRunner, UnknownBackendError, make_runner
from teatree.eval.coverage import CoverageReport, skill_eval_coverage
from teatree.eval.discovery import discover_specs
from teatree.eval.models import EvalSpec
from teatree.eval.negative_control import NegativeControlOutcome, run_negative_control
from teatree.eval.parallel import DEFAULT_PARALLEL, run_specs
from teatree.eval.regression_corpus import RegressionReport, run_regression_corpus
from teatree.eval.report import ScenarioResult, evaluate
from teatree.eval.transcript_conformance import InvariantResult
from teatree.eval.trigger_qa import TriggerQAReport, run_trigger_qa
from teatree.utils.django_bootstrap import ensure_django


def _relative_source(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return path.name


def build_scenarios_table(specs: list[EvalSpec]) -> Table:
    table = Table(title="Eval scenarios", show_lines=False)
    table.add_column("Name", style="bold")
    table.add_column("Scenario")
    table.add_column("Agent")
    table.add_column("File", overflow="ellipsis", no_wrap=True)
    table.add_column("Asserts", justify="right")
    for spec in specs:
        table.add_row(
            spec.name,
            spec.scenario,
            spec.agent_path,
            _relative_source(spec.source_path),
            str(len(spec.matchers)),
        )
    return table


def trigger_lane(report: TriggerQAReport) -> LaneResult:
    return LaneResult(
        name="skill-triggers",
        cost="free",
        passed=report.ok,
        skipped=False,
        detail=f"{len(report.checks)} checks, {len(report.failures)} failed",
    )


def regression_lane(report: RegressionReport) -> LaneResult:
    return LaneResult(
        name="pinned-regressions",
        cost="free",
        passed=report.ok,
        skipped=False,
        detail=f"{len(report.results)} checks, {len(report.failures)} failed",
    )


def coverage_lane(report: CoverageReport) -> LaneResult:
    gap_names = ", ".join(r.skill for r in report.gaps)
    detail = (
        f"{len(report.rows)} skills, {len(report.gaps)} uncovered (warn-first): {gap_names}"
        if report.gaps
        else f"{len(report.rows)} skills, all covered or eval_exempt"
    )
    return LaneResult(name="skill-coverage", cost="free", passed=True, skipped=False, detail=detail)


def negative_control_lane(outcome: NegativeControlOutcome) -> LaneResult:
    detail = "harness caught the planted violation" if outcome.caught else "harness MISSED the planted violation"
    return LaneResult(
        name="negative-control",
        cost="free",
        passed=outcome.caught,
        skipped=False,
        detail=detail,
    )


def transcript_replay_lane(results: list[InvariantResult] | None) -> LaneResult:
    if results is None:
        return LaneResult(
            name="transcript-replay",
            cost="free",
            passed=True,
            skipped=True,
            detail="no session transcript in scope",
        )
    failed = sum(1 for result in results if not result.ok)
    return LaneResult(
        name="transcript-replay",
        cost="free",
        passed=failed == 0,
        skipped=False,
        detail=f"{len(results)} invariants, {failed} violated",
    )


def run_ai_lane(
    specs: list[EvalSpec], *, backend: str, target_dir: Path, parallel: int = DEFAULT_PARALLEL
) -> LaneResult:
    try:
        runner = make_runner(backend, transcript_dir=target_dir)
    except UnknownBackendError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from None
    if isinstance(runner, SubscriptionTranscriptRunner) and not _any_transcript_present(specs, runner):
        _emit_subscription_recipe(specs, target_dir)
        return _ai_lane_result([], backend=backend, graded=False)
    runs = run_specs(runner, specs, parallel=parallel)
    results = list(starmap(evaluate, zip(specs, runs, strict=True)))
    return _ai_lane_result(results, backend=backend, graded=True)


def _any_transcript_present(specs: list[EvalSpec], runner: SubscriptionTranscriptRunner) -> bool:
    return any(runner.transcript_path(spec).is_file() for spec in specs)


def _emit_subscription_recipe(specs: list[EvalSpec], target_dir: Path) -> None:
    typer.echo(render_subscription_text(build_subscription_manifest(specs, target_dir)))
    typer.echo(
        "\nNo subscription transcripts on disk — the AI lane was not graded. Produce them "
        "in-session (no API spend) via the /t3:running-evals skill (it dispatches a sub-agent per "
        "scenario and captures each with `t3 eval capture-subagent`), then re-run `t3 eval all`.",
        err=True,
    )


#: One-line, plain-language instruction for enabling the AI behavioural lane.
AI_LANE_SETUP_HINT = (
    "no in-session transcripts; run `t3 eval capture-subagent` "
    "(see /t3:running-evals) or use `--backend sdk` with an API key"
)


def _ai_lane_result(results: list[ScenarioResult], *, backend: str, graded: bool) -> LaneResult:
    if not graded:
        return LaneResult(
            name="ai-eval",
            cost="subscription",
            passed=True,
            skipped=True,
            detail="not run — no transcripts to grade",
            setup_hint=AI_LANE_SETUP_HINT,
        )
    executed = [r for r in results if not r.skipped]
    failed = sum(1 for r in executed if not r.passed)
    cost = "metered (sdk)" if backend != SUBSCRIPTION_BACKEND else "subscription"
    return LaneResult(
        name="ai-eval",
        cost=cost,
        passed=failed == 0,
        skipped=not executed,
        detail=f"{len(executed)} graded, {failed} failed, {len(results) - len(executed)} skipped",
        setup_hint=AI_LANE_SETUP_HINT if not executed else None,
    )


def hint_missing_transcripts(runner: SubscriptionTranscriptRunner, missing: list[EvalSpec]) -> None:
    if not missing:
        return
    typer.echo(
        f"\n{len(missing)} scenario(s) skipped — no subscription transcript on disk.",
        err=True,
    )
    for spec in missing:
        typer.echo(f"  - {spec.name}: expected transcript at {runner.transcript_path(spec)}", err=True)
    names = " ".join(spec.name for spec in missing)
    typer.echo(
        "Produce them with the subscription (no API spend): run "
        f"`t3 eval prepare-subscription {names}` for each scenario's prompt + path, drive each prompt "
        "via an in-session sub-agent (the /t3:running-evals skill does this), then capture its "
        "trajectory with `t3 eval capture-subagent <scenario>` and re-run "
        "`t3 eval run --backend subscription`.",
        err=True,
    )


def build_summary_table(lanes: Iterable[LaneResult]) -> Table:
    table = Table(title="Eval suite — all lanes", show_lines=False)
    table.add_column("Lane", style="bold")
    table.add_column("Cost")
    table.add_column("Status", justify="right")
    table.add_column("Detail")
    for lane in lanes:
        color = "yellow" if lane.skipped else ("green" if lane.passed else "red")
        detail = f"{lane.detail} ({lane.setup_hint})" if lane.needs_setup else lane.detail
        table.add_row(lane.name, lane.cost, f"[{color}]{lane.status}[/{color}]", detail)
    return table


def _full_suite_docker_passthrough(
    *, backend: str, free_only: bool, strict: bool, parallel: int = DEFAULT_PARALLEL
) -> list[str]:
    passthrough = ["all"]
    if free_only:
        passthrough.append("--free-only")
    if backend != SUBSCRIPTION_BACKEND:
        passthrough += ["--backend", backend]
    if strict:
        passthrough.append("--strict")
    if parallel != DEFAULT_PARALLEL:
        passthrough += ["--parallel", str(parallel)]
    return passthrough


def run_full_suite(  # noqa: PLR0913 — the single eval-suite chokepoint: each keyword-only param maps 1:1 to a public bare-`t3 eval` / `t3 eval all` flag. The arg list IS the CLI contract.
    *,
    backend: str,
    transcript_dir: Path | None,
    free_only: bool,
    docker: bool,
    strict: bool,
    parallel: int = DEFAULT_PARALLEL,
) -> None:
    """The single eval-suite chokepoint: run every lane and render one summary.

    Both the bare ``t3 eval`` default and the explicit ``t3 eval all`` subcommand
    call this so the no-arg path and the named path execute byte-for-byte the
    same suite. The five free deterministic lanes (skill-triggers, skill-coverage,
    pinned-regressions, negative-control, transcript-replay) always run;
    skill-coverage is warn-first and transcript-replay SKIPs when no real session
    transcript is in scope (a missing run is not a violation). The AI lane grades
    subscription-produced transcripts when present and NEVER silently shells the
    metered ``claude -p`` runner; ``--backend sdk`` is the explicit metered opt-in.
    ``parallel`` runs that many AI-lane scenarios concurrently (wall-clock only).

    The run always ends with a plain-language verdict (:func:`build_verdict`) a
    non-expert can read: ``✅ ALL GOOD`` / ``❌ PROBLEMS FOUND`` / a ``✅`` for the
    deterministic part plus a ``⚠️ NOT RUN … not yet validated`` for any lane that
    was skipped because it needs setup (the AI lane with no transcripts / no key).
    A real FAIL always exits non-zero (fail-loud). A setup-skip stays exit 0 by
    default (the clarity is in the verdict text, not a confusing non-zero); pass
    ``--strict`` to make a setup-skipped lane exit non-zero for CI use.
    """
    if docker:
        passthrough = _full_suite_docker_passthrough(
            backend=backend, free_only=free_only, strict=strict, parallel=parallel
        )
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
        lanes.append(run_ai_lane(discover_specs(), backend=backend, target_dir=target_dir, parallel=parallel))
    Console().print(build_summary_table(lanes))
    print_verdict(lanes)
    real_failure = any(not lane.passed and not lane.skipped for lane in lanes)
    strict_failure = strict and any(lane.needs_setup for lane in lanes)
    if real_failure or strict_failure:
        sys.exit(1)
