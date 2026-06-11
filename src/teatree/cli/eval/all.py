"""``t3 eval list`` table render + ``t3 eval all`` lane orchestration.

The seven free deterministic lanes (skill-triggers, skill-coverage, pinned-regressions,
negative-control, transcript-replay, corpus-grade, skill-command-validity) always run;
skill-coverage is warn-first (reports a gap, never FAILs in Phase A), transcript-replay
surfaces as a SKIP when no real session transcript is in scope (never a FAIL), corpus-grade
grades the ground-truth corpus deterministically (judge-oracle entries skip), and
skill-command-validity (#550 Tier-1) FAILs on a backticked ``t3 …`` in a SKILL.md that no
longer resolves against the live CLI registry (the "no stale references" rule). The
AI/trajectory lane grades subscription-produced transcripts when they exist on disk; with
none it emits the subscription manifest plus the in-session recipe and NEVER silently shells
the metered ``claude -p`` runner. ``--backend sdk`` is the explicit metered opt-in. The
metered path also runs the ADVISORY skill-prose-judge lane (#550 Tier-3) — it scores each
SKILL.md's prose via the existing ``ClaudeJudge`` seam and nominates the weakest skill but
NEVER fails the suite (judge-only is advisory; matcher/structural lanes gate CI).
"""

import dataclasses
import sys
import time
from collections.abc import Callable, Iterable
from itertools import starmap
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from teatree.cli.eval.corpus import corpus_grade_lane, grade_shipped_corpus
from teatree.cli.eval.docker import DockerUnavailableError, run_eval_in_docker
from teatree.cli.eval.metered_routing import should_route_to_docker, warn_local_metered
from teatree.cli.eval.run_modes import build_subscription_manifest, render_subscription_text
from teatree.cli.eval.skill_command_lane import skill_command_validity_lane, validate_shipped_skill_commands
from teatree.cli.eval.skill_prose_lane import run_prose_judge, skill_prose_judge_lane
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
    # Lane / Cost / Status never wrap — the lane name is the identity a reader
    # (and the lane-name assertions) keys on, so a long Detail must never squeeze
    # it onto two lines. Only Detail wraps.
    table.add_column("Lane", style="bold", no_wrap=True)
    table.add_column("Cost", no_wrap=True)
    table.add_column("Status", justify="right", no_wrap=True)
    table.add_column("Detail")
    for lane in lanes:
        color = "yellow" if lane.skipped else ("green" if lane.passed else "red")
        detail = f"{lane.detail} ({lane.setup_hint})" if lane.needs_setup else lane.detail
        table.add_row(lane.name, lane.cost, f"[{color}]{lane.status}[/{color}]", detail)
    return table


def _full_suite_docker_passthrough(
    *, backend: str, free_only: bool, strict: bool, parallel: int = DEFAULT_PARALLEL, html_path: Path | None = None
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
    if html_path is not None:
        passthrough += ["--html", str(html_path)]
    return passthrough


def _timed(build: Callable[[], LaneResult]) -> LaneResult:
    """Run a lane builder and stamp the lane with its wall-clock duration."""
    started = time.monotonic()
    lane = build()
    return dataclasses.replace(lane, duration_s=time.monotonic() - started)


#: Shared by the bare-``t3 eval`` callback (in app.py) and the ``t3 eval all``
#: command (in all_command.py) — identical full-suite ``--strict`` flag.
STRICT_HELP = (
    "Exit non-zero when a lane was SKIPPED for setup reasons (the AI behavioural lane with no "
    "transcripts / no key) — for CI, where 'not yet validated' must fail. Default leaves a "
    "setup-skip green (the caveat is in the verdict text, not a confusing non-zero)."
)


def run_full_suite(  # noqa: PLR0913 — the single eval-suite chokepoint: each keyword-only param maps 1:1 to a public bare-`t3 eval` / `t3 eval all` flag. The arg list IS the CLI contract.
    *,
    backend: str,
    transcript_dir: Path | None,
    free_only: bool,
    docker: bool,
    strict: bool,
    local: bool = False,
    parallel: int = DEFAULT_PARALLEL,
    html_path: Path | None = None,
) -> None:
    """The single eval-suite chokepoint: run every lane and render one summary.

    Both the bare ``t3 eval`` default and the explicit ``t3 eval all`` subcommand
    call this so the no-arg path and the named path execute byte-for-byte the
    same suite. The seven free deterministic lanes (skill-triggers, skill-coverage,
    pinned-regressions, negative-control, transcript-replay, corpus-grade,
    skill-command-validity) always run; skill-coverage is warn-first,
    transcript-replay SKIPs when no real session transcript is in scope (a missing
    run is not a violation), corpus-grade grades the ground-truth corpus
    deterministically (judge entries skip), and skill-command-validity FAILs on a
    stale ``t3 …`` reference in a SKILL.md. The AI lane grades subscription-produced
    transcripts when present and NEVER silently shells the metered ``claude -p``
    runner; ``--backend sdk`` is the explicit metered opt-in. The metered path also
    runs the ADVISORY skill-prose-judge lane (never fails the suite).
    ``parallel`` runs that many AI-lane scenarios concurrently (wall-clock only).
    ``html_path`` writes a self-contained whole-suite HTML report (CI artifact).

    The run always ends with a plain-language verdict (:func:`build_verdict`) a
    non-expert can read: ``✅ ALL GOOD`` / ``❌ PROBLEMS FOUND`` / a ``✅`` for the
    deterministic part plus a ``⚠️ NOT RUN … not yet validated`` for any lane that
    was skipped because it needs setup (the AI lane with no transcripts / no key).
    A real FAIL always exits non-zero (fail-loud). A setup-skip stays exit 0 by
    default (the clarity is in the verdict text, not a confusing non-zero); pass
    ``--strict`` to make a setup-skipped lane exit non-zero for CI use.
    """
    # The metered SDK AI lane + the live prose-judge bill the API, so the whole
    # suite defaults to the CI container when they are in scope (`--backend sdk`,
    # not `--free-only`) — exactly like `eval run` / `eval benchmark`. `--docker`
    # forces the container for any backend; `--local` is the explicit host escape;
    # `--free-only` runs only the host-safe deterministic lanes, so it is never
    # metered. A metered host run must never happen silently.
    metered = backend != SUBSCRIPTION_BACKEND and not free_only
    if docker or should_route_to_docker(metered=metered, local=local):
        passthrough = _full_suite_docker_passthrough(
            backend=backend, free_only=free_only, strict=strict, parallel=parallel, html_path=html_path
        )
        try:
            raise typer.Exit(code=run_eval_in_docker(passthrough))
        except DockerUnavailableError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from None
    if local:
        warn_local_metered(metered=metered)
    ensure_django()
    target_dir = transcript_dir or Path.cwd()
    lanes = [
        _timed(lambda: trigger_lane(run_trigger_qa())),
        _timed(lambda: coverage_lane(skill_eval_coverage())),
        _timed(lambda: regression_lane(run_regression_corpus())),
        _timed(lambda: negative_control_lane(run_negative_control())),
        _timed(lambda: transcript_replay_lane(replay_transcript_for_all())),
        _timed(lambda: corpus_grade_lane(grade_shipped_corpus())),
        _timed(lambda: skill_command_validity_lane(validate_shipped_skill_commands())),
    ]
    if not free_only:
        # The AI lane runs first. It is itself backend-gated: the default
        # subscription backend grades on-disk transcripts (no API spend) and never
        # shells the metered runner, so it is safe on the bare-`t3 eval` path.
        lanes.append(
            _timed(lambda: run_ai_lane(discover_specs(), backend=backend, target_dir=target_dir, parallel=parallel))
        )
    if metered:
        # The ADVISORY Tier-3 prose-judge fires the LIVE metered ClaudeJudge, so it
        # is gated on the explicit metered opt-in (`--backend sdk`), NOT merely on
        # `not --free-only` — bare `t3 eval` (default subscription, advertised "no
        # API spend") must never silently bill the judge, and `--free-only` (which
        # is never metered) drops it. skill_prose_judge_lane always returns
        # passed=True, so a low prose score never fails the suite.
        lanes.append(_timed(lambda: skill_prose_judge_lane(run_prose_judge())))
    Console().print(build_summary_table(lanes))
    print_verdict(lanes)
    if html_path is not None:
        _write_html_report(lanes, html_path)
    real_failure = any(not lane.passed and not lane.skipped for lane in lanes)
    strict_failure = strict and any(lane.needs_setup for lane in lanes)
    if real_failure or strict_failure:
        sys.exit(1)


def _write_html_report(lanes: list[LaneResult], html_path: Path) -> None:
    from teatree.cli.eval.suite_html import render_suite_html  # noqa: PLC0415

    html_path.write_text(render_suite_html(lanes), encoding="utf-8")
