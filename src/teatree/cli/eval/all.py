"""``t3 eval list`` table render + bare-``t3 eval`` full-suite lane orchestration.

The seven free deterministic lanes (skill-triggers, skill-coverage, pinned-regressions,
negative-control, transcript-replay, corpus-grade, skill-command-validity) always run;
skill-coverage is warn-first (reports a gap, never FAILs in Phase A), transcript-replay
surfaces as a SKIP when no real session transcript is in scope (never a FAIL), corpus-grade
grades the ground-truth corpus deterministically (judge-oracle entries skip), and
skill-command-validity (#550 Tier-1) FAILs on a backticked ``t3 …`` in a SKILL.md that no
longer resolves against the live CLI registry (the "no stale references" rule). The
AI/trajectory lane grades already-recorded transcripts when they exist on disk ($0 extra);
with none it emits the transcript manifest plus the in-session recipe and NEVER silently
runs a model. ``--backend api`` is the explicit fresh-run opt-in. The fresh-run path also
runs the ADVISORY skill-prose-judge lane (#550 Tier-3) — it scores each SKILL.md's prose via
the existing ``ClaudeJudge`` seam and nominates the weakest skill but NEVER fails the suite
(judge-only is advisory; matcher/structural lanes gate CI).
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
from teatree.cli.eval.metered_routing import should_route_to_docker
from teatree.cli.eval.run_modes import build_transcript_manifest, render_transcript_text
from teatree.cli.eval.skill_command_lane import skill_command_validity_lane, validate_shipped_skill_commands
from teatree.cli.eval.skill_prose_lane import run_prose_judge, skill_prose_judge_lane
from teatree.cli.eval.transcript_replay import replay_transcript_for_all
from teatree.cli.eval.verdict import LaneResult, print_verdict
from teatree.eval.backends import TRANSCRIPT_BACKEND, TranscriptRunner, UnknownBackendError, make_runner
from teatree.eval.coverage import CoverageReport, skill_eval_coverage
from teatree.eval.discovery import discover_specs
from teatree.eval.models import EvalSpec
from teatree.eval.negative_control import NegativeControlOutcome, run_negative_control
from teatree.eval.parallel import DEFAULT_PARALLEL, run_specs
from teatree.eval.regression_corpus import RegressionReport, run_regression_corpus
from teatree.eval.report import ScenarioResult, evaluate
from teatree.eval.skip_guard import UnmeteredApiRunError, assert_api_run_was_metered
from teatree.eval.transcript_conformance import InvariantResult
from teatree.eval.trigger_qa import TriggerQAReport, run_trigger_qa
from teatree.llm.anthropic_limits import CreditExhaustedError
from teatree.utils.django_bootstrap import ensure_django


def _relative_source(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return path.name


def build_scenarios_table(specs: list[EvalSpec]) -> Table:
    table = Table(title="Eval scenarios", show_lines=False)
    # ``min_width`` floors the Name column at the longest name so a piped/non-TTY
    # console (default width 80) can neither wrap nor truncate it. A CI discovery-
    # assertion greps ``t3 eval list`` for a scenario NAME on one line; without
    # this floor a long scenario name truncates to an ellipsis and the grep
    # falsely reports broken overlay wiring.
    longest_name = max((len(spec.name) for spec in specs), default=0)
    table.add_column("Name", style="bold", no_wrap=True, min_width=longest_name)
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


@dataclasses.dataclass(frozen=True)
class AiLaneOutcome:
    """The AI lane's summary plus the graded per-scenario results.

    ``results`` is threaded out so the suite chokepoint can sum each run's
    ``cost_usd`` and run the unmetered-$0 guard (:func:`assert_api_run_was_metered`)
    on the fresh-run (``--backend api``) path — the same vacuous-green guard the
    ``eval run`` boundary applies via ``RunGuards.api_metered``. It is empty when
    the lane did not grade (no transcripts on the transcript path).
    """

    lane: LaneResult
    results: list[ScenarioResult]


def run_ai_lane(
    specs: list[EvalSpec], *, backend: str, target_dir: Path, parallel: int = DEFAULT_PARALLEL
) -> AiLaneOutcome:
    try:
        runner = make_runner(backend, transcript_dir=target_dir)
    except UnknownBackendError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from None
    if isinstance(runner, TranscriptRunner) and not _any_transcript_present(specs, runner):
        _emit_transcript_recipe(specs, target_dir)
        return AiLaneOutcome(lane=_ai_lane_result([], backend=backend, graded=False), results=[])
    try:
        runs = run_specs(runner, specs, parallel=parallel)
    except CreditExhaustedError as exc:
        typer.echo(f"ABORTED: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    results = list(starmap(evaluate, zip(specs, runs, strict=True)))
    return AiLaneOutcome(lane=_ai_lane_result(results, backend=backend, graded=True), results=results)


def _any_transcript_present(specs: list[EvalSpec], runner: TranscriptRunner) -> bool:
    return any(runner.transcript_path(spec).is_file() for spec in specs)


def _emit_transcript_recipe(specs: list[EvalSpec], target_dir: Path) -> None:
    typer.echo(render_transcript_text(build_transcript_manifest(specs, target_dir)))
    typer.echo(
        "\nNo recorded transcripts on disk — the AI lane was not graded. Produce them "
        "in-session ($0 extra) via the /t3:running-evals skill (it dispatches a sub-agent per "
        "scenario and captures each with `t3 eval capture-subagent`), then re-run `t3 eval`.",
        err=True,
    )


#: One-line, plain-language instruction for enabling the AI behavioural lane.
AI_LANE_SETUP_HINT = (
    "no in-session transcripts; run `t3 eval capture-subagent` "
    "(see /t3:running-evals) or use `--backend api` (metered on ANTHROPIC_API_KEY)"
)


def _ai_lane_result(results: list[ScenarioResult], *, backend: str, graded: bool) -> LaneResult:
    if not graded:
        return LaneResult(
            name="ai-eval",
            cost="transcript ($0)",
            passed=True,
            skipped=True,
            detail="not run — no transcripts to grade",
            setup_hint=AI_LANE_SETUP_HINT,
        )
    executed = [r for r in results if not r.skipped]
    failed = sum(1 for r in executed if not r.passed)
    cost = "api (fresh run)" if backend != TRANSCRIPT_BACKEND else "transcript ($0)"
    return LaneResult(
        name="ai-eval",
        cost=cost,
        passed=failed == 0,
        skipped=not executed,
        detail=f"{len(executed)} graded, {failed} failed, {len(results) - len(executed)} skipped",
        setup_hint=AI_LANE_SETUP_HINT if not executed else None,
    )


def hint_missing_transcripts(runner: TranscriptRunner, missing: list[EvalSpec]) -> None:
    if not missing:
        return
    typer.echo(
        f"\n{len(missing)} scenario(s) skipped — no recorded transcript on disk.",
        err=True,
    )
    for spec in missing:
        typer.echo(f"  - {spec.name}: expected transcript at {runner.transcript_path(spec)}", err=True)
    names = " ".join(spec.name for spec in missing)
    typer.echo(
        "Produce them in-session ($0 extra): run "
        f"`t3 eval prepare-transcript {names}` for each scenario's prompt + path, drive each prompt "
        "via an in-session sub-agent (the /t3:running-evals skill does this), then capture its "
        "trajectory with `t3 eval capture-subagent <scenario>` and re-run "
        "`t3 eval run --backend transcript`.",
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
    *, backend: str, free_only: bool, strict: bool, parallel: int = DEFAULT_PARALLEL
) -> list[str]:
    # The bare `t3 eval` callback IS the full suite (no subcommand), so the
    # in-container re-invocation passes only the flags — `all` was removed.
    passthrough: list[str] = []
    if free_only:
        passthrough.append("--free-only")
    if backend != TRANSCRIPT_BACKEND:
        passthrough += ["--backend", backend]
    if strict:
        passthrough.append("--strict")
    if parallel != DEFAULT_PARALLEL:
        passthrough += ["--parallel", str(parallel)]
    return passthrough


def _timed(build: Callable[[], LaneResult]) -> LaneResult:
    """Run a lane builder and stamp the lane with its wall-clock duration."""
    started = time.monotonic()
    lane = build()
    return dataclasses.replace(lane, duration_s=time.monotonic() - started)


#: Shared by the bare-``t3 eval`` callback (in app.py) — the full-suite
#: ``--strict`` flag help.
STRICT_HELP = (
    "Exit non-zero when a lane was SKIPPED for setup reasons (the AI behavioural lane with no "
    "transcripts / no key) — for CI, where 'not yet validated' must fail. Default leaves a "
    "setup-skip green (the caveat is in the verdict text, not a confusing non-zero)."
)


# ast-grep-ignore: ac-django-no-complexity-suppressions
def run_full_suite(  # noqa: PLR0913 — the single eval-suite chokepoint: each keyword-only param maps 1:1 to a public bare-`t3 eval` flag. The arg list IS the CLI contract.
    *,
    backend: str,
    transcript_dir: Path | None,
    free_only: bool,
    docker: bool,
    strict: bool,
    parallel: int = DEFAULT_PARALLEL,
) -> None:
    """The single eval-suite chokepoint: run every lane and render one summary.

    The bare ``t3 eval`` default calls this. The seven free deterministic lanes
    (skill-triggers, skill-coverage, pinned-regressions, negative-control,
    transcript-replay, corpus-grade, skill-command-validity) always run;
    skill-coverage is warn-first, transcript-replay SKIPs when no real session
    transcript is in scope (a missing run is not a violation), corpus-grade grades
    the ground-truth corpus deterministically (judge entries skip), and
    skill-command-validity FAILs on a stale ``t3 …`` reference in a SKILL.md. The
    AI lane grades already-recorded transcripts when present ($0 extra) and NEVER
    silently runs a model; ``--backend api`` is the explicit fresh-run opt-in. The
    fresh-run path also runs the ADVISORY skill-prose-judge lane (never fails the
    suite). ``parallel`` runs that many AI-lane scenarios concurrently (wall-clock
    only).

    The run always ends with a plain-language verdict (:func:`build_verdict`) a
    non-expert can read: ``✅ ALL GOOD`` / ``❌ PROBLEMS FOUND`` / a ``✅`` for the
    deterministic part plus a ``⚠️ NOT RUN … not yet validated`` for any lane that
    was skipped because it needs setup (the AI lane with no transcripts / no key).
    A real FAIL always exits non-zero (fail-loud). A setup-skip stays exit 0 by
    default (the clarity is in the verdict text, not a confusing non-zero); pass
    ``--strict`` to make a setup-skipped lane exit non-zero for CI use.
    """
    # The fresh-run SDK AI lane + the live prose-judge run a model, so the whole
    # suite defaults to the CI container when they are in scope (`--backend api`,
    # not `--free-only`) — exactly like `eval run` / `eval benchmark`. `--docker`
    # forces the container for any backend; `--free-only` runs only the host-safe
    # deterministic lanes, so it never runs a model.
    metered = backend != TRANSCRIPT_BACKEND and not free_only
    if docker or should_route_to_docker(metered=metered, local=False):
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
        _timed(lambda: trigger_lane(run_trigger_qa())),
        _timed(lambda: coverage_lane(skill_eval_coverage())),
        _timed(lambda: regression_lane(run_regression_corpus())),
        _timed(lambda: negative_control_lane(run_negative_control())),
        _timed(lambda: transcript_replay_lane(replay_transcript_for_all())),
        _timed(lambda: corpus_grade_lane(grade_shipped_corpus())),
        _timed(lambda: skill_command_validity_lane(validate_shipped_skill_commands())),
    ]
    ai_results: list[ScenarioResult] = []
    if not free_only:
        # The AI lane runs first. It is itself backend-gated: the default
        # transcript backend grades on-disk transcripts ($0 extra) and never runs
        # a model, so it is safe on the bare-`t3 eval` path.
        started = time.monotonic()
        ai_outcome = run_ai_lane(discover_specs(), backend=backend, target_dir=target_dir, parallel=parallel)
        ai_results = ai_outcome.results
        lanes.append(dataclasses.replace(ai_outcome.lane, duration_s=time.monotonic() - started))
    if metered:
        # The ADVISORY Tier-3 prose-judge fires the LIVE ClaudeJudge, so it is gated
        # on the explicit fresh-run opt-in (`--backend api`), NOT merely on
        # `not --free-only` — bare `t3 eval` (default transcript, $0 extra) must
        # never silently run a model, and `--free-only` drops it.
        # skill_prose_judge_lane always returns passed=True, so a low prose score
        # never fails the suite.
        lanes.append(_timed(lambda: skill_prose_judge_lane(run_prose_judge())))
    Console().print(build_summary_table(lanes))
    print_verdict(lanes)
    # The fresh-run (`--backend api`) AI lane: an api run that executed scenarios
    # but recorded $0 never actually executed (the `--bare` OAuth bug — the model
    # authenticated as nothing, made zero tool calls, recorded nothing). Mirror the
    # `eval run` boundary's RunGuards.api_metered: fail loud rather than read the
    # green AI lane as validated. The transcript backend runs no model by design,
    # so the guard short-circuits there (`backend != "api"`).
    _assert_metered_api_ai_lane(backend=backend, results=ai_results)
    if _suite_should_fail(lanes, strict=strict, metered=metered):
        sys.exit(1)


def _suite_should_fail(lanes: list[LaneResult], *, strict: bool, metered: bool) -> bool:
    """Decide the suite exit: a real lane failure, or a setup-skip under strict.

    A metered suite (``--backend api``) IMPLIES strict: a needs-setup (skipped)
    lane under an explicit metered backend is a fail-loud, not a silent green —
    a metered run that could not set a lane up has no business reporting green
    (§4c). Without a metered backend, only ``--strict`` escalates a setup-skip.
    """
    real_failure = any(not lane.passed and not lane.skipped for lane in lanes)
    strict_failure = (strict or metered) and any(lane.needs_setup for lane in lanes)
    return real_failure or strict_failure


def _assert_metered_api_ai_lane(*, backend: str, results: list[ScenarioResult]) -> None:
    """Turn an executed-but-$0 api AI lane RED — the suite mirror of ``RunGuards.api_metered``."""
    executed = sum(1 for r in results if not r.skipped)
    try:
        assert_api_run_was_metered(
            backend=backend,
            executed=executed,
            total_cost_usd=sum(r.run.cost_usd for r in results),
        )
    except UnmeteredApiRunError as exc:
        typer.echo(str(exc), err=True)
        sys.exit(1)
