"""Persist eval results and diff a run against a recorded baseline (#1160).

The harness (``runner.py`` + ``report.py``/``pass_at_k.py``) produces a verdict
per scenario but keeps nothing. This module is the thin bridge from those
in-memory verdicts to the durable :class:`~teatree.core.models.EvalRunRecord`
ledger, plus the cross-run regression diff the ``--baseline`` CLI mode renders.

It deals only in plain value objects (:class:`ScenarioOutcome`) so it is equally
drivable from a single-trial run (``report.ScenarioResult``) and a multi-trial
run (``pass_at_k.PassAtKResult``) — the two CLI paths adapt their own result
type into an outcome and hand it here, so the store never imports either result
type and stays a leaf of the eval package.
"""

import dataclasses
import uuid

from teatree.utils import git
from teatree.utils.run import CommandFailedError


@dataclasses.dataclass(frozen=True)
class ScenarioOutcome:
    """A model-agnostic verdict for one scenario, ready to persist."""

    scenario: str
    model: str
    passed: bool
    score: float
    trials: int
    skipped: bool = False


@dataclasses.dataclass(frozen=True)
class RegressionEntry:
    """One scenario's baseline-vs-current comparison."""

    scenario: str
    model: str
    baseline_score: float
    current_score: float

    @property
    def regressed(self) -> bool:
        return self.current_score < self.baseline_score

    @property
    def improved(self) -> bool:
        return self.current_score > self.baseline_score


@dataclasses.dataclass(frozen=True)
class RegressionReport:
    """The full baseline diff for one recorded run."""

    run_id: str
    entries: tuple[RegressionEntry, ...]
    new_scenarios: tuple[str, ...]

    @property
    def regressions(self) -> tuple[RegressionEntry, ...]:
        return tuple(e for e in self.entries if e.regressed)

    @property
    def improvements(self) -> tuple[RegressionEntry, ...]:
        return tuple(e for e in self.entries if e.improved)

    @property
    def ok(self) -> bool:
        return not self.regressions


def new_run_id() -> str:
    return uuid.uuid4().hex


def current_git_sha() -> str:
    try:
        return git.head_sha()
    except (CommandFailedError, OSError):
        return ""


def record_run(
    outcomes: list[ScenarioOutcome],
    *,
    run_id: str | None = None,
    git_sha: str | None = None,
) -> str:
    """Persist *outcomes* under one ``run_id`` and return it.

    A single ``run_id`` spans every scenario/model the run executed so the run
    is reconstructable by grouping. ``git_sha`` is resolved from ``HEAD`` once
    when not supplied, so all rows of a run share the same provenance.
    """
    from teatree.core.models import EvalRunRecord  # noqa: PLC0415

    resolved_run_id = run_id or new_run_id()
    resolved_sha = current_git_sha() if git_sha is None else git_sha
    for outcome in outcomes:
        EvalRunRecord.objects.record_scenario(
            run_id=resolved_run_id,
            scenario=outcome.scenario,
            model=outcome.model,
            passed=outcome.passed,
            score=outcome.score,
            trials=outcome.trials,
            git_sha=resolved_sha,
            skipped=outcome.skipped,
        )
    return resolved_run_id


def diff_against_baseline(run_id: str) -> RegressionReport:
    """Compare a recorded run against each model's preceding recorded run.

    For every ``(scenario, model)`` in the run, the baseline is the same triple
    from that model's most recent run that is strictly older than this one.
    A scenario with no baseline is reported as new (never a regression).
    Skipped baseline or current rows are ignored for the score comparison.
    """
    from teatree.core.models import EvalRunRecord  # noqa: PLC0415

    current_rows = list(EvalRunRecord.objects.filter(run_id=run_id))
    entries: list[RegressionEntry] = []
    new_scenarios: list[str] = []
    baseline_by_model: dict[str, dict[str, EvalRunRecord]] = {}
    for row in current_rows:
        if row.model not in baseline_by_model:
            baseline_rows = EvalRunRecord.objects.baseline_for_model(row.model, before_run_id=run_id)
            baseline_by_model[row.model] = {b.scenario: b for b in baseline_rows}
        baseline = baseline_by_model[row.model].get(row.scenario)
        if baseline is None or baseline.skipped or row.skipped:
            if baseline is None and not row.skipped:
                new_scenarios.append(row.scenario)
            continue
        entries.append(
            RegressionEntry(
                scenario=row.scenario,
                model=row.model,
                baseline_score=baseline.score,
                current_score=row.score,
            )
        )
    return RegressionReport(
        run_id=run_id,
        entries=tuple(sorted(entries, key=lambda e: (e.model, e.scenario))),
        new_scenarios=tuple(sorted(new_scenarios)),
    )
