"""Persisted run-store for behavioral eval results (#1160 baseline/history).

The eval harness runs each scenario against a model and aggregates a verdict.
Without a durable record every run is amnesiac: a scenario that regressed since
last week looks identical to one that was always red, and a model-matrix
comparison has nothing to compare against. :class:`EvalRunRecord` is the ledger
that makes cross-run regression observable.

One row per ``(run_id, scenario, model)`` triple — a single ``t3 eval run`` is
one ``run_id`` (a uuid hex) spanning every scenario it executed, so a run is
reconstructable by grouping on ``run_id`` and the latest run for a model is the
implicit baseline. ``score`` is the pass-rate over ``trials`` (1.0 for a single
green trial, ``passes/trials`` under pass@k), so a regression is a numeric drop,
not just a green→red flip.

Mirrors the single-identity marker shape of
:class:`teatree.core.models.mini_loop_marker.MiniLoopMarker` crossed with the
grouped-by-natural-key ledger of
:class:`teatree.core.models.implemented_issue_marker.ImplementedIssueMarker`.
"""

import datetime as dt
from typing import TYPE_CHECKING, ClassVar

from django.db import models
from django.utils import timezone

if TYPE_CHECKING:
    from django.db.models import QuerySet


class EvalRunRecordManager(models.Manager["EvalRunRecord"]):
    """Read/write surface for persisting and querying eval run history."""

    def record_scenario(  # noqa: PLR0913 — each kwarg is one persisted column of the run-store row.
        self,
        *,
        run_id: str,
        scenario: str,
        model: str,
        passed: bool,
        score: float,
        trials: int,
        git_sha: str = "",
        skipped: bool = False,
        recorded_at: dt.datetime | None = None,
    ) -> "EvalRunRecord":
        """Upsert one scenario result inside a run.

        Keyed on ``(run_id, scenario, model)`` so re-recording the same triple
        (a retried trial within one run) updates in place rather than
        duplicating. ``recorded_at`` defaults to now and is shared across a run
        so every row of one ``run_id`` carries the same wall-clock timestamp.
        """
        row, _ = self.update_or_create(
            run_id=run_id,
            scenario=scenario,
            model=model,
            defaults={
                "passed": passed,
                "score": score,
                "trials": trials,
                "git_sha": git_sha,
                "skipped": skipped,
                "recorded_at": recorded_at or timezone.now(),
            },
        )
        return row

    def runs(self, *, model: str | None = None, limit: int = 20) -> list["RunSummary"]:
        """Return the most recent runs (newest first) as :class:`RunSummary`.

        A run is the set of rows sharing a ``run_id``. ``model`` filters to runs
        that touched that model. ``limit`` caps the number of distinct runs.
        """
        qs = self.all() if model is None else self.filter(model=model)
        summaries: dict[str, RunSummary] = {}
        for row in qs.order_by("-recorded_at"):
            summary = summaries.get(row.run_id)
            if summary is None:
                if len(summaries) >= limit:
                    continue
                summary = RunSummary(run_id=row.run_id, recorded_at=row.recorded_at)
                summaries[row.run_id] = summary
            summary.absorb(row)
        return sorted(summaries.values(), key=lambda s: s.recorded_at, reverse=True)

    def baseline_for_model(self, model: str, *, before_run_id: str | None = None) -> "QuerySet[EvalRunRecord]":
        """Return the most recent recorded run's rows for *model* as the baseline.

        When *before_run_id* is given, the most recent run strictly older than
        that run is used — so a run can be compared against the run that
        preceded it rather than against itself. Returns an empty queryset when no
        baseline exists.
        """
        candidates = self.filter(model=model)
        if before_run_id is not None:
            current = candidates.filter(run_id=before_run_id).order_by("recorded_at").first()
            if current is not None:
                candidates = candidates.filter(recorded_at__lt=current.recorded_at)
        latest = candidates.order_by("-recorded_at").first()
        if latest is None:
            return self.none()
        return self.filter(model=model, run_id=latest.run_id)


class RunSummary:
    """Aggregate view of one run (all rows sharing a ``run_id``)."""

    def __init__(self, *, run_id: str, recorded_at: dt.datetime) -> None:
        self.run_id = run_id
        self.recorded_at = recorded_at
        self.models: set[str] = set()
        self.total = 0
        self.passed = 0
        self.skipped = 0

    def absorb(self, row: "EvalRunRecord") -> None:
        self.models.add(row.model)
        self.total += 1
        if row.skipped:
            self.skipped += 1
        elif row.passed:
            self.passed += 1
        self.recorded_at = min(self.recorded_at, row.recorded_at)

    @property
    def failed(self) -> int:
        return self.total - self.passed - self.skipped


class EvalRunRecord(models.Model):
    """One scenario's result within one eval run against one model."""

    run_id = models.CharField(max_length=64)
    scenario = models.CharField(max_length=128)
    model = models.CharField(max_length=64)
    passed = models.BooleanField(default=False)
    skipped = models.BooleanField(default=False)
    score = models.FloatField(default=0.0)
    trials = models.PositiveSmallIntegerField(default=1)
    git_sha = models.CharField(max_length=64, blank=True, default="")
    recorded_at = models.DateTimeField(default=timezone.now)

    objects: ClassVar[EvalRunRecordManager] = EvalRunRecordManager()

    class Meta:
        db_table = "teatree_eval_run_record"
        ordering: ClassVar = ["-recorded_at"]
        indexes: ClassVar = [
            models.Index(fields=["model", "-recorded_at"], name="eval_rec_model_recorded"),
            models.Index(fields=["run_id"], name="eval_rec_run_id"),
        ]
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["run_id", "scenario", "model"],
                name="uniq_eval_run_scenario_model",
            ),
        ]

    def __str__(self) -> str:
        verdict = "skip" if self.skipped else ("pass" if self.passed else "fail")
        return f"eval-rec<{self.scenario}@{self.model}:{verdict}>"
