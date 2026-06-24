"""The single-trial ``t3 eval run`` execution path.

Held apart from :mod:`teatree.cli.eval.app` (which is at its module-LOC cap) so
the command body stays the typer surface + routing while this owns the one-trial
shape: build the runner, run every spec once, render, drop the per-run artifacts
(transcript HTML + sanitized summary), run the no-coverage guards, and apply the
history/regression gates. The multi-trial and model-matrix shapes live in
:mod:`teatree.cli.eval.multi_trial`; this is their single-pass sibling.
"""

import dataclasses
import sys
from pathlib import Path

import typer
from claude_agent_sdk.types import EffortLevel

from teatree.cli.eval.all import hint_missing_transcripts
from teatree.cli.eval.app_helpers import write_single_trial_reports
from teatree.cli.eval.run_modes import DEFAULT_COST_REGRESSION_TOLERANCE, RunGuards, finalize_single_run
from teatree.eval.backends import TRANSCRIPT_BACKEND, TranscriptRunner, UnknownBackendError, make_runner
from teatree.eval.models import EvalSpec
from teatree.eval.parallel import run_specs
from teatree.eval.report import JudgeGrader, evaluate, render_html, render_json, render_text


@dataclasses.dataclass(frozen=True)
class SingleTrialGates:
    """The persistence + regression-gate flags the single-trial finalize consumes."""

    persist: bool
    baseline: bool
    gate_regressions: bool
    gate_cost_regression: bool
    cost_regression_tolerance: float = DEFAULT_COST_REGRESSION_TOLERANCE
    gate_cost_bounds: bool = False


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
) -> None:
    """Run every spec once, render, drop the per-run artifacts, and gate the result.

    The artifacts (full transcript HTML + sanitized summary md) are written from
    THIS run's results — no re-run — and BEFORE any guard/gate can exit, so a red
    run still drops both the diagnostic transcript and the publishable summary the
    workflow appends to ``$GITHUB_STEP_SUMMARY``.
    """
    try:
        runner = make_runner(
            backend,
            max_turns_override=max_turns,
            transcript_dir=transcript_dir,
            require_executed=require_executed,
            max_budget_usd=max_budget_usd,
            effort=effort,
        )
    except UnknownBackendError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from None
    runs = run_specs(runner, specs, parallel=parallel)
    results = [evaluate(spec, run, judge=grader) for spec, run in zip(specs, runs, strict=True)]
    renderers = {"json": render_json, "html": render_html}
    typer.echo(renderers.get(output_format, render_text)(results))
    write_single_trial_reports(results, transcript_html=transcript_html, summary_md=summary_md)
    if backend == TRANSCRIPT_BACKEND and isinstance(runner, TranscriptRunner):
        hint_missing_transcripts(runner, [spec for spec, r in zip(specs, results, strict=True) if r.skipped])
    executed = sum(1 for r in results if not r.skipped)
    RunGuards.executed(executed=executed, collected=len(specs), required=require_executed)
    RunGuards.sdk_metered(backend=backend, executed=executed, results=results)
    RunGuards.judge_metered(judge_requested=judge, results=results)
    if finalize_single_run(
        results,
        specs=specs,
        max_turns=max_turns,
        persist=gates.persist,
        baseline=gates.baseline,
        gate_regressions=gates.gate_regressions,
        gate_cost_regression=gates.gate_cost_regression,
        cost_regression_tolerance=gates.cost_regression_tolerance,
        gate_cost_bounds=gates.gate_cost_bounds,
    ):
        sys.exit(1)
