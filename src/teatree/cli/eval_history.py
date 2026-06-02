"""Rendering and baseline-marking for ``t3 eval history``.

Reads the run-history ledger and renders recent runs plus each run's
per-scenario pass-rate. The aggregation itself lives on the models
(``EvalRunRecord.pass_rates``); this module only formats it for the CLI.
"""

import json
from typing import TypedDict

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
    pass_rates: list[HistoryPassRate]


def mark_run_baseline(run_id: int) -> bool:
    run = EvalRunRecord.objects.filter(pk=run_id).first()
    if run is None:
        return False
    run.mark_baseline()
    return True


def _run_dict(run: EvalRunRecord) -> HistoryRun:
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
        pass_rates=[
            HistoryPassRate(
                scenario=r.scenario_name, model=r.model, passed=r.passed, total=r.total, pass_rate=r.pass_rate
            )
            for r in run.pass_rates()
        ],
    )


def render_history_json(runs: list[EvalRunRecord]) -> str:
    return json.dumps({"runs": [_run_dict(run) for run in runs]}, indent=2)


def render_history_text(runs: list[EvalRunRecord]) -> str:
    if not runs:
        return "(no eval runs recorded)"
    lines: list[str] = []
    for run in runs:
        tag = " [baseline]" if run.is_baseline else ""
        lines.append(
            f"#{run.pk} {run.started_at.isoformat()} model={run.model}{tag} "
            f"— {run.passed} passed, {run.failed} failed, {run.skipped} skipped (of {run.total})"
        )
        lines.extend(
            f"    {r.scenario_name}{f' [{r.model}]' if r.model else ''}: {r.passed}/{r.total} ({r.pass_rate:.0%})"
            for r in run.pass_rates()
        )
    return "\n".join(lines)
