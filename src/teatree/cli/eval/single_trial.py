"""The single-trial ``t3 eval run`` execution path.

Held apart from :mod:`teatree.cli.eval.app` (which is at its module-LOC cap) so
the command body stays the typer surface + routing while this owns the one-trial
shape: build the runner, run every spec once, render, drop the per-run artifacts
(transcript HTML + sanitized summary), run the no-coverage guards, and apply the
history/regression gates. The multi-trial and model-matrix shapes live in
:mod:`teatree.cli.eval.multi_trial`; this is their single-pass sibling.
"""

import dataclasses
import json
import sys
from collections.abc import Callable
from pathlib import Path

import typer
from claude_agent_sdk.types import EffortLevel

from teatree.cli.eval.all import hint_missing_transcripts
from teatree.cli.eval.app_helpers import RunReportPaths
from teatree.cli.eval.escalate import (
    EscalationConfig,
    EscalationOutcome,
    EscalationReport,
    TrialRunner,
    describe_classification,
    escalate_failures,
    render_escalation_markdown,
)
from teatree.cli.eval.run_modes import DEFAULT_COST_REGRESSION_TOLERANCE, RunGuards, finalize_single_run
from teatree.eval.api_errors import UsageLimitReachedError
from teatree.eval.artifact_redaction import write_artifact
from teatree.eval.backends import (
    API_BACKEND,
    FRESH_RUN_BACKENDS,
    TRANSCRIPT_BACKEND,
    ApiRunnerParams,
    EvalRunner,
    TranscriptRunner,
    UnknownBackendError,
    make_runner,
)
from teatree.eval.models import CachedPass, EvalRun, EvalSpec
from teatree.eval.parallel import UsageLimitLatch, is_usage_limit_skip, run_specs, usage_limit_skip
from teatree.eval.report import JudgeGrader, ScenarioResult, evaluate, render_json, render_text
from teatree.eval.skip_guard import MEASURED_NOTHING_EXIT_CODE
from teatree.eval.summary_json import SummaryCacheData, write_summary_json
from teatree.llm.anthropic_limits import CreditExhaustedError

__all__ = ["EscalationConfig", "SingleTrialGates", "make_escalation_runner", "run_single_trial"]


@dataclasses.dataclass(frozen=True)
class SingleTrialGates:
    """The persistence + regression-gate flags the single-trial finalize consumes."""

    persist: bool
    baseline: bool
    gate_regressions: bool
    gate_cost_regression: bool
    cost_regression_tolerance: float = DEFAULT_COST_REGRESSION_TOLERANCE
    gate_cost_bounds: bool = False


@dataclasses.dataclass(frozen=True)
class VerdictReuse:
    cached: list[CachedPass]
    cached_specs: list[EvalSpec]
    content_keys: dict[str, str]


def make_escalation_runner(
    *, backend: str, max_budget_usd: float, effort: EffortLevel | None, require_executed: bool
) -> EvalRunner:
    """Build the runner the escalation re-runs a failed scenario through.

    Escalation always RUNS the model fresh, so it MIRRORS the lane's own *backend*
    whenever that backend can produce a new trial (:data:`FRESH_RUN_BACKENDS`) and
    rewrites onto ``api`` only for ``transcript`` — the one lane that merely replays
    an already-recorded run. Forcing ``api`` unconditionally predates the CLI-free
    ``anthropic_api`` backend and silently moved ITS escalation onto the ``claude``
    CLI child that lane exists to avoid; where no CLI is provisioned every escalation
    trial then skips, which is the ``0/k escalation trials`` symptom.

    ``require_executed`` is forwarded so an escalation that CANNOT run fails loud
    rather than skipping: a skipped re-run disambiguates nothing, and a silent one
    hides the trial-1 failure the escalation was spent to confirm.

    Held apart so the single-trial test harness can stub the escalation runner
    without a live model.
    """
    return make_runner(
        backend if backend in FRESH_RUN_BACKENDS else API_BACKEND,
        ApiRunnerParams(max_budget_usd=max_budget_usd, effort=effort, require_executed=require_executed),
    )


def _make_single_runner(backend: str, params: ApiRunnerParams, transcript_dir: Path | None) -> EvalRunner:
    try:
        return make_runner(backend, params, transcript_dir=transcript_dir)
    except UnknownBackendError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from None


def _emit_single_results(results: list[ScenarioResult], cached: list[CachedPass], output_format: str) -> None:
    renderers = {"json": render_json, "html": lambda items: RunReportPaths.render_cached_html(items, cached)}
    rendered = renderers.get(output_format, render_text)(results)
    if output_format == "json" and cached:
        payload = json.loads(rendered)
        payload["cached"] = [item.as_json() for item in cached]
        typer.echo(json.dumps(payload))
        return
    typer.echo(rendered, nl=output_format != "html")
    if output_format == "text":
        for item in cached:
            typer.echo(f"CACHED PASS {item.name} (key {item.content_key}, from run {item.run_url})")


def _persist_all_cached(cached_specs: list[EvalSpec], gates: SingleTrialGates, max_turns: int | None) -> None:
    if gates.persist:
        finalize_single_run(
            [],
            specs=[],
            cached_specs=cached_specs,
            max_turns=max_turns,
            persist=True,
            baseline=False,
            gate_regressions=False,
            gate_cost_regression=False,
            cost_regression_tolerance=gates.cost_regression_tolerance,
        )


def _usage_limit_result(spec: EvalSpec) -> ScenarioResult:
    return ScenarioResult(spec=spec, run=usage_limit_skip(spec.name), matcher_results=(), skipped=True)


class _JudgeLimitLatch:
    def __init__(self, grader: JudgeGrader | None) -> None:
        self.grader = grader
        self.limited = False

    def grade(self, spec: EvalSpec, run: Callable[[], EvalRun]) -> ScenarioResult:
        if self.limited:
            return _usage_limit_result(spec)
        try:
            return evaluate(spec, run(), judge=self.grader)
        except UsageLimitReachedError:
            self.limited = True
            return _usage_limit_result(spec)


def _grade_runs(specs: list[EvalSpec], runs: list[EvalRun], grader: JudgeGrader | None) -> list[ScenarioResult]:
    latch = _JudgeLimitLatch(grader)
    return [latch.grade(spec, lambda run=run: run) for spec, run in zip(specs, runs, strict=True)]


def _escalation_trial_runner(latch: UsageLimitLatch, grader: JudgeGrader | None) -> TrialRunner:
    judge_latch = _JudgeLimitLatch(grader)

    def trial(spec: EvalSpec) -> ScenarioResult:
        return judge_latch.grade(spec, lambda: latch.run(spec))

    return trial


# ast-grep-ignore: ac-django-no-complexity-suppressions
def run_single_trial(  # noqa: PLR0913 — each kwarg threads one resolved `eval run` value into the single-pass path.
    specs: list[EvalSpec],
    *,
    backend: str,
    max_turns: int | None,
    transcript_dir: Path | None,
    require_executed: bool,
    max_budget_usd: float,
    effort: EffortLevel | None,
    parallel: int,
    output_format: str,
    grader: JudgeGrader | None,
    judge: bool,
    transcript_html: Path | None,
    summary_md: Path | None,
    gates: SingleTrialGates,
    escalation: EscalationConfig | None = None,
    summary_json: Path | None = None,
    reuse: VerdictReuse | None = None,
) -> None:
    """Run every spec once, render, drop the per-run artifacts, and gate the result.

    The artifacts (full transcript HTML + sanitized summary md) are written from
    THIS run's results — no re-run — and BEFORE any guard/gate can exit, so a red
    run still drops both the diagnostic transcript and the publishable summary the
    workflow appends to ``$GITHUB_STEP_SUMMARY``.

    ``escalation`` (the ``--escalate-on-fail`` PR-lane path) turns a single-trial
    FAILURE into a re-run rather than an immediate red: each failed scenario runs
    ``escalate_trials`` more times. A recovered scenario clears the lane gate but
    is recorded as FLAKY, separate from clean PASS.
    """
    reuse = reuse or VerdictReuse([], [], {})
    cached, cached_specs, content_keys = reuse.cached, reuse.cached_specs, reuse.content_keys
    reports = RunReportPaths(transcript_html, summary_md, summary_json)
    if not specs and cached:
        _emit_single_results([], cached, output_format)
        reports.write_single_trial([], cached=cached, content_keys=content_keys, backend=backend)
        _persist_all_cached(cached_specs, gates, max_turns)
        return
    runner = _make_single_runner(
        backend,
        ApiRunnerParams(
            max_turns_override=max_turns,
            require_executed=require_executed,
            max_budget_usd=max_budget_usd,
            effort=effort,
        ),
        transcript_dir,
    )
    try:
        runs = run_specs(runner, specs, parallel=parallel)
    except CreditExhaustedError as exc:
        typer.echo(f"ABORTED: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    results = _grade_runs(specs, runs, grader)
    _emit_single_results(results, cached, output_format)
    reports.write_single_trial(results, cached=cached, content_keys=content_keys, backend=backend)
    if backend == TRANSCRIPT_BACKEND and isinstance(runner, TranscriptRunner):
        hint_missing_transcripts(runner, [spec for spec, r in zip(specs, results, strict=True) if r.skipped])
    _exit_when_the_usage_limit_ran_nothing(results)
    executed = sum(1 for r in results if not r.skipped)
    cap_exhausted = getattr(runner, "budget_exhausted", False) or any(r.coverage_incomplete for r in results)
    RunGuards.hooks_registered(results)
    RunGuards.executed(executed=executed, collected=len(specs), required=require_executed and not cap_exhausted)
    RunGuards.api_metered(backend=backend, executed=executed, results=results)
    RunGuards.judge_metered(judge_requested=judge, results=results)
    if escalation is not None:
        escalation_runner = make_escalation_runner(
            backend=backend, max_budget_usd=max_budget_usd, effort=effort, require_executed=require_executed
        )

        latch = UsageLimitLatch(escalation_runner, limited=bool(_refused_by_the_usage_limit(results)))
        escalation_report = _escalate_and_gate(
            results, escalation=escalation, trial=_escalation_trial_runner(latch, grader), summary_md=summary_md
        )
        if summary_json is not None:
            write_summary_json(
                results,
                summary_json,
                escalations={outcome.spec_name: outcome.classification for outcome in escalation_report.outcomes},
                cache=SummaryCacheData(content_keys, cached, backend),
            )
        if escalation_report.hard_red:
            sys.exit(1)
    elif finalize_single_run(
        results,
        specs=specs,
        cached_specs=cached_specs,
        max_turns=max_turns,
        persist=gates.persist,
        baseline=gates.baseline,
        gate_regressions=gates.gate_regressions,
        gate_cost_regression=gates.gate_cost_regression,
        cost_regression_tolerance=gates.cost_regression_tolerance,
        gate_cost_bounds=gates.gate_cost_bounds,
    ):
        sys.exit(1)
    if cap_exhausted:
        _exit_suite_budget_exhausted(results)
    # LAST, so the ledger keeps the record of the run that measured nothing. This is
    # the only lane where the all-skipped guard above can be disarmed (`--trials` and
    # `--models` arm it unconditionally), so it is the only place a suite could grade
    # nothing and still exit 0.
    _exit_when_the_usage_limit_refused_any(results)
    RunGuards.declined_is_not_a_pass(executed=executed, collected=len(specs))


def _exit_suite_budget_exhausted(results: list[ScenarioResult]) -> None:
    skipped = sum(r.skipped for r in results)
    typer.echo(f"coverage incomplete: suite budget exhausted; {skipped}/{len(results)} scenarios skipped", err=True)
    raise typer.Exit(code=MEASURED_NOTHING_EXIT_CODE)


def _refused_by_the_usage_limit(results: list[ScenarioResult]) -> list[ScenarioResult]:
    return [r for r in results if is_usage_limit_skip(r.run)]


def _exit_when_the_usage_limit_ran_nothing(results: list[ScenarioResult]) -> None:
    """Exit 75 when nothing executed and the usage limit is why — ahead of the all-skipped guard's hard red."""
    refused = _refused_by_the_usage_limit(results)
    if refused and all(r.skipped for r in results):
        _exit_did_not_run(refused, collected=len(results))


def _exit_when_the_usage_limit_refused_any(results: list[ScenarioResult]) -> None:
    """Exit 75 when nothing failed but the usage limit kept some scenarios from running: never green."""
    refused = _refused_by_the_usage_limit(results)
    if refused:
        _exit_did_not_run(refused, collected=len(results))


def _exit_did_not_run(refused: list[ScenarioResult], *, collected: int) -> None:
    typer.echo(
        f"eval run DID NOT COMPLETE {len(refused)} of {collected} scenario(s): the subscription usage limit "
        f"interrupted evaluation ({refused[0].run.terminal_reason}). Nothing measured failed. "
        f"Exiting {MEASURED_NOTHING_EXIT_CODE} (tolerated, never green).",
        err=True,
    )
    raise typer.Exit(code=MEASURED_NOTHING_EXIT_CODE)


def _escalate_and_gate(
    results: list[ScenarioResult],
    *,
    escalation: EscalationConfig,
    trial: TrialRunner,
    summary_md: Path | None,
) -> EscalationReport:
    """Re-run the single-trial failures, append the escalation section, gate on confirmed.

    Each scenario that failed trial 1 is re-run ``escalate_trials`` times through
    *trial* (a fresh metered runner closure); a scenario that recovers on any trial
    is FLAKY (gate cleared, never clean PASS), one that fails every escalation
    trial is confirmed (red), and
    one whose trials all skipped is unresolved (also red — nothing re-proved it). The
    escalation section is appended to the sanitized ``--summary-md`` dashboard so
    the PR's ``$GITHUB_STEP_SUMMARY`` shows the flaky/confirmed split.
    """
    report = escalate_failures(results, trial, escalate_trials=escalation.escalate_trials)
    typer.echo(_render_escalation_text(report))
    if summary_md is not None:
        section = render_escalation_markdown(report)
        if section:
            write_artifact(summary_md, "\n" + section, append=True)
    return report


def _render_escalation_text(report: EscalationReport) -> str:
    if not report.outcomes:
        return "ESCALATION: no scenario failed the single trial — nothing to escalate."
    lines = ["ESCALATION:"]
    lines.extend(
        f"  {describe_classification(outcome).upper()} {outcome.spec_name} ({_describe_trials(outcome)})"
        for outcome in report.outcomes
    )
    return "\n".join(lines)


def _describe_trials(outcome: EscalationOutcome) -> str:
    """The trial tally, spelled out for ``unresolved`` where a bare 0/k reads as a loss.

    An escalation that never ran also tallies 0 passes, and printing it the same way
    as a fought-and-lost 0/k hides the one fact a reader needs: the failure was never
    re-tested.
    """
    if outcome.classification == "unresolved":
        return f"all {outcome.trials} escalation trials skipped — the trial-1 failure stands"
    return f"{outcome.passes}/{outcome.trials} escalation trials"
