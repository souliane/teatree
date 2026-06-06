"""``t3 eval list`` table render + ``t3 eval all`` lane orchestration.

The five free deterministic lanes (trigger-qa, skill-coverage, regression,
negative-control, transcript-replay) always run; skill-coverage is warn-first
(reports a gap, never FAILs in Phase A) and transcript-replay surfaces as a SKIP
when no real session transcript is in scope (never a FAIL). The AI/trajectory lane grades
subscription-produced transcripts when they exist on disk; with none it emits the
subscription manifest plus the in-session recipe and NEVER silently shells the
metered ``claude -p`` runner. ``--backend sdk`` is the explicit metered opt-in.
"""

import dataclasses
from collections.abc import Iterable
from pathlib import Path

import typer
from rich.table import Table

from teatree.cli.eval_run_modes import build_subscription_manifest, render_subscription_text
from teatree.eval.backends import SUBSCRIPTION_BACKEND, SubscriptionTranscriptRunner, UnknownBackendError, make_runner
from teatree.eval.coverage import CoverageReport
from teatree.eval.models import EvalSpec
from teatree.eval.negative_control import NegativeControlOutcome
from teatree.eval.regression_corpus import RegressionReport
from teatree.eval.report import ScenarioResult, evaluate
from teatree.eval.transcript_conformance import InvariantResult
from teatree.eval.trigger_qa import TriggerQAReport


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


@dataclasses.dataclass(frozen=True)
class LaneResult:
    """One eval lane's outcome in the unified ``t3 eval all`` summary."""

    name: str
    cost: str
    passed: bool
    skipped: bool
    detail: str

    @property
    def status(self) -> str:
        if self.skipped:
            return "SKIP"
        return "PASS" if self.passed else "FAIL"


def trigger_lane(report: TriggerQAReport) -> LaneResult:
    return LaneResult(
        name="trigger-qa",
        cost="free",
        passed=report.ok,
        skipped=False,
        detail=f"{len(report.checks)} checks, {len(report.failures)} failed",
    )


def regression_lane(report: RegressionReport) -> LaneResult:
    return LaneResult(
        name="regression",
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


def run_ai_lane(specs: list[EvalSpec], *, backend: str, target_dir: Path) -> LaneResult:
    try:
        runner = make_runner(backend, transcript_dir=target_dir)
    except UnknownBackendError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from None
    if isinstance(runner, SubscriptionTranscriptRunner) and not _any_transcript_present(specs, runner):
        _emit_subscription_recipe(specs, target_dir)
        return _ai_lane_result([], backend=backend, graded=False)
    results = [evaluate(spec, runner.run(spec)) for spec in specs]
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


def _ai_lane_result(results: list[ScenarioResult], *, backend: str, graded: bool) -> LaneResult:
    if not graded:
        return LaneResult(
            name="ai-eval",
            cost="subscription",
            passed=True,
            skipped=True,
            detail="no transcripts — see /t3:running-evals to produce them in-session",
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
        table.add_row(lane.name, lane.cost, f"[{color}]{lane.status}[/{color}]", lane.detail)
    return table
