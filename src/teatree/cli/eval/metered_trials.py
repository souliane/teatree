"""Pass@k progress and usage-limit verdicts for metered eval lanes."""

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Never

import typer

from teatree.eval.api_errors import UsageLimitReachedError
from teatree.eval.artifact_redaction import write_artifact
from teatree.eval.models import EvalSpec
from teatree.eval.pass_at_k import PassAtKResult, run_pass_at_k
from teatree.eval.report import ScenarioResult
from teatree.eval.surface import is_advisory


class UsageLimitAfterFailureError(UsageLimitReachedError):
    pass


def run_trials_with_limit_evidence(
    spec: EvalSpec, trial: Callable[[EvalSpec], ScenarioResult], *, k: int, require: str
) -> PassAtKResult:
    observed: list[ScenarioResult] = []

    def tracked(candidate: EvalSpec) -> ScenarioResult:
        result = trial(candidate)
        observed.append(result)
        return result

    try:
        return run_pass_at_k(spec, tracked, k=k, require=require)
    except UsageLimitReachedError as exc:
        if any(result.measured_failure for result in observed):
            raise UsageLimitAfterFailureError from exc
        raise


def _emit_progress(line: str) -> None:
    sys.stderr.write(f"{line}\n")
    sys.stderr.flush()


def _run_scenario_with_progress(
    spec: EvalSpec,
    trial: Callable[[EvalSpec], ScenarioResult],
    *,
    trials: int,
    require: str,
    position: tuple[int, int],
) -> PassAtKResult:
    index, total = position
    _emit_progress(f"RUN  [{index}/{total}] {spec.name} (k={trials}, require={require})")
    result = run_trials_with_limit_evidence(spec, trial, k=trials, require=require)
    verdict = "SKIP" if result.skipped else ("PASS" if result.ok else "FAIL")
    _emit_progress(f"DONE [{index}/{total}] {spec.name}: {verdict} ({result.passes}/{result.trials})")
    return result


def park_on_usage_limit(
    *, summary_md: Path | None = None, html_out: Path | None = None, failed_before_limit: bool = False
) -> Never:
    message = (
        "RED: eval failed before the usage limit; remaining coverage is unmeasured."
        if failed_before_limit
        else "PARKED: usage limit reached; remaining eval coverage is unmeasured."
    )
    typer.echo(message, err=True)
    if summary_md is not None:
        write_artifact(summary_md, message + "\n")
    if html_out is not None:
        write_artifact(html_out, f"<html><body><p>{message}</p></body></html>\n")
    raise typer.Exit(code=1 if failed_before_limit else 75)


def run_pass_at_k_results(
    specs: list[EvalSpec],
    trial: Callable[[EvalSpec], ScenarioResult],
    *,
    trials: int,
    require: str,
    summary_md: Path | None,
) -> list[PassAtKResult]:
    results: list[PassAtKResult] = []
    for index, spec in enumerate(specs, start=1):
        try:
            results.append(
                _run_scenario_with_progress(spec, trial, trials=trials, require=require, position=(index, len(specs)))
            )
        except UsageLimitReachedError as exc:
            failed = (isinstance(exc, UsageLimitAfterFailureError) and not is_advisory(spec)) or any(
                result.verdict == "fail" and not is_advisory(specs[position]) for position, result in enumerate(results)
            )
            park_on_usage_limit(summary_md=summary_md, failed_before_limit=failed)
    return results
