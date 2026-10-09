"""Rendering and baseline-marking for ``t3 eval history``.

Reads the run-history ledger and renders recent runs plus each run's
per-scenario pass-rate. The aggregation itself lives on the models
(``EvalRunRecord.pass_rates``); this module only formats it for the CLI.
"""

import json
from typing import TYPE_CHECKING, TypedDict

import typer

from teatree.cli._format_opts import require_valid_format
from teatree.eval.skip_guard import graded_nothing
from teatree.utils.django_bootstrap import ensure_django

if TYPE_CHECKING:
    from teatree.core.models import EvalRunRecord


class HistoryPassRate(TypedDict):
    scenario: str
    model: str
    passed: int
    total: int
    pass_rate: float


class HistoryRun(TypedDict):
    id: int
    started_at: str
    model: str
    suite: str
    git_sha: str
    is_baseline: bool
    total: int
    passed: int
    failed: int
    skipped: int
    executed: int
    measured_nothing: bool
    pass_rates: list[HistoryPassRate]


def _executed(run: "EvalRunRecord") -> int:
    return run.total - run.skipped


def _measured_nothing(run: "EvalRunRecord") -> bool:
    return graded_nothing(collected=run.total, executed=_executed(run))


def mark_run_baseline(run_id: int) -> bool:
    from teatree.core.models import EvalRunRecord  # noqa: PLC0415 — deferred: ORM import needs the app registry

    run = EvalRunRecord.objects.filter(pk=run_id).first()
    if run is None:
        return False
    run.mark_baseline()
    return True


def _run_dict(run: "EvalRunRecord") -> HistoryRun:
    return HistoryRun(
        id=run.pk,
        started_at=run.started_at.isoformat(),
        model=run.model,
        suite=run.suite,
        git_sha=run.git_sha,
        is_baseline=run.is_baseline,
        total=run.total,
        passed=run.passed,
        failed=run.failed,
        skipped=run.skipped,
        executed=_executed(run),
        measured_nothing=_measured_nothing(run),
        pass_rates=[
            HistoryPassRate(
                scenario=r.scenario_name, model=r.model, passed=r.passed, total=r.total, pass_rate=r.pass_rate
            )
            for r in run.pass_rates()
        ],
    )


def render_history_json(runs: list["EvalRunRecord"]) -> str:
    return json.dumps({"runs": [_run_dict(run) for run in runs]}, indent=2)


def _tally(run: "EvalRunRecord") -> str:
    """What the run establishes — a graded tally, or that it established nothing.

    A run that graded none of what it collected renders ``0 passed, 0 failed, N
    skipped``, which is shaped exactly like a clean sweep and was read as one for
    five consecutive weeks. The two facts are opposite, so they get different words.
    """
    if _measured_nothing(run):
        return f"— MEASURED NOTHING: 0 of {run.total} executed ({run.skipped} skipped)"
    return f"— {run.passed} passed, {run.failed} failed, {run.skipped} skipped (of {run.total})"


def render_history_text(runs: list["EvalRunRecord"]) -> str:
    if not runs:
        return "(no eval runs recorded)"
    lines: list[str] = []
    for run in runs:
        tag = " [baseline]" if run.is_baseline else ""
        lines.append(f"#{run.pk} {run.started_at.isoformat()} model={run.model}{tag} {_tally(run)}")
        lines.extend(
            f"    {r.scenario_name}{f' [{r.model}]' if r.model else ''}: {r.passed}/{r.total} ({r.pass_rate:.0%})"
            for r in run.pass_rates()
        )
    return "\n".join(lines)


def history_command(
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
    ensure_django()
    require_valid_format(output_format)
    from teatree.core.models import EvalRunRecord  # noqa: PLC0415 — deferred: ORM import needs the app registry

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
