"""Durable run-history and baseline ledger for the behavioral eval harness.

Every ``t3 eval run`` invocation is recorded as one :class:`EvalRunRecord`
(the run group) plus one :class:`EvalScenarioResult` per scenario per trial.
The harness today is single-trial, but the schema carries ``trial`` from the
start so the later k>=3 / pass@k phase persists without a migration.

Each scenario result stores both signals the runner produces: the
*trajectory* signal (the captured tool calls) and the *side-effect* signal
(the terminal reason + error flag). Grading verdict and per-matcher detail
are stored alongside so a historical run is fully reconstructable without
re-invoking the model.

Fat Model: the regression-diff, pass-rate aggregation, and baseline lookup
that the later model-regression mode reads are queryset/manager methods here,
not in callers. ``EvalRunRecord.regression_diff`` is the trivial
baseline-vs-candidate diff the Geert deliverable builds on.
"""

import dataclasses
from typing import Any, ClassVar, TypedDict

from django.db import models
from django.utils import timezone


class TrajectoryToolCall(TypedDict):
    name: str
    input: dict[str, Any]
    turn: int


class MatcherDetail(TypedDict):
    kind: str
    tool: str
    arg_path: str
    operator: str
    value: str
    passed: bool


class EvalVerdict(models.TextChoices):
    PASS = "pass", "Pass"
    FAIL = "fail", "Fail"
    SKIP = "skip", "Skip"


@dataclasses.dataclass(frozen=True)
class ScenarioPassRate:
    scenario_name: str
    total: int
    passed: int

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0


@dataclasses.dataclass(frozen=True)
class ScenarioRegression:
    scenario_name: str
    baseline_pass_rate: float
    candidate_pass_rate: float

    @property
    def delta(self) -> float:
        return self.candidate_pass_rate - self.baseline_pass_rate

    @property
    def regressed(self) -> bool:
        return self.candidate_pass_rate < self.baseline_pass_rate


class EvalRunQuerySet(models.QuerySet["EvalRunRecord"]):
    def baselines(self) -> "EvalRunQuerySet":
        return self.filter(is_baseline=True)

    def for_model(self, model: str) -> "EvalRunQuerySet":
        return self.filter(model=model)

    def latest_baseline(self) -> "EvalRunRecord | None":
        return self.baselines().order_by("-started_at").first()


class EvalRunManager(models.Manager["EvalRunRecord"]):
    def get_queryset(self) -> EvalRunQuerySet:
        return EvalRunQuerySet(self.model, using=self._db)

    def for_model(self, model: str) -> EvalRunQuerySet:
        return self.get_queryset().for_model(model)

    def latest_baseline(self) -> "EvalRunRecord | None":
        return self.get_queryset().latest_baseline()

    def record(
        self,
        *,
        model: str,
        suite: str = "",
        overlay: str = "",
        max_turns_override: int | None = None,
        is_baseline: bool = False,
    ) -> "EvalRunRecord":
        return self.create(
            model=model,
            suite=suite,
            overlay=overlay,
            max_turns_override=max_turns_override,
            is_baseline=is_baseline,
        )


class EvalRunRecord(models.Model):
    """One ``t3 eval run`` invocation across all selected scenarios."""

    started_at = models.DateTimeField(default=timezone.now)
    model = models.CharField(max_length=64)
    suite = models.CharField(max_length=128, blank=True, default="")
    overlay = models.CharField(max_length=64, blank=True, default="")
    max_turns_override = models.IntegerField(null=True, blank=True, default=None)
    is_baseline = models.BooleanField(default=False)

    objects: ClassVar[EvalRunManager] = EvalRunManager()

    class Meta:
        db_table = "teatree_eval_run"
        ordering: ClassVar = ["-started_at"]
        indexes: ClassVar = [
            models.Index(fields=["model", "started_at"], name="eval_run_model_started_idx"),
            models.Index(fields=["is_baseline", "started_at"], name="eval_run_baseline_idx"),
        ]

    def __str__(self) -> str:
        tag = "baseline" if self.is_baseline else "run"
        return f"eval-{tag}<{self.pk}:{self.model}@{self.started_at.isoformat()}>"

    @property
    def results(self) -> "EvalScenarioResultQuerySet":
        return EvalScenarioResult.objects.filter(run=self)

    @property
    def total(self) -> int:
        return self.results.count()

    @property
    def passed(self) -> int:
        return self.results.filter(verdict=EvalVerdict.PASS).count()

    @property
    def failed(self) -> int:
        return self.results.filter(verdict=EvalVerdict.FAIL).count()

    @property
    def skipped(self) -> int:
        return self.results.filter(verdict=EvalVerdict.SKIP).count()

    def mark_baseline(self) -> None:
        type(self).objects.for_model(self.model).baselines().exclude(pk=self.pk).update(is_baseline=False)
        self.is_baseline = True
        self.save(update_fields=["is_baseline"])

    def record_scenario(  # noqa: PLR0913 — per-scenario ledger record API; each kwarg is a documented field.
        self,
        *,
        scenario_name: str,
        verdict: str,
        trial: int = 0,
        terminal_reason: str = "",
        is_error: bool = False,
        tool_calls: list[TrajectoryToolCall] | None = None,
        matcher_details: list[MatcherDetail] | None = None,
    ) -> "EvalScenarioResult":
        return EvalScenarioResult.objects.create(
            run=self,
            scenario_name=scenario_name,
            trial=trial,
            verdict=verdict,
            terminal_reason=terminal_reason,
            is_error=is_error,
            tool_calls=tool_calls or [],
            matcher_details=matcher_details or [],
        )

    def pass_rates(self) -> list[ScenarioPassRate]:
        return self.results.pass_rates()

    @classmethod
    def regression_diff(cls, *, baseline: "EvalRunRecord", candidate: "EvalRunRecord") -> list[ScenarioRegression]:
        baseline_rates = {r.scenario_name: r.pass_rate for r in baseline.pass_rates()}
        candidate_rates = {r.scenario_name: r.pass_rate for r in candidate.pass_rates()}
        scenarios = sorted(set(baseline_rates) | set(candidate_rates))
        return [
            ScenarioRegression(
                scenario_name=name,
                baseline_pass_rate=baseline_rates.get(name, 0.0),
                candidate_pass_rate=candidate_rates.get(name, 0.0),
            )
            for name in scenarios
        ]


class EvalScenarioResultQuerySet(models.QuerySet["EvalScenarioResult"]):
    def graded(self) -> "EvalScenarioResultQuerySet":
        return self.exclude(verdict=EvalVerdict.SKIP)

    def pass_rates(self) -> list[ScenarioPassRate]:
        counts: dict[str, list[int]] = {}
        for name, verdict in self.graded().values_list("scenario_name", "verdict"):
            slot = counts.setdefault(name, [0, 0])
            slot[0] += 1
            if verdict == EvalVerdict.PASS:
                slot[1] += 1
        return [
            ScenarioPassRate(scenario_name=name, total=total, passed=passed)
            for name, (total, passed) in sorted(counts.items())
        ]


EvalScenarioResultManager = models.Manager.from_queryset(EvalScenarioResultQuerySet)


class EvalScenarioResult(models.Model):
    """One scenario's grading verdict within one trial of an :class:`EvalRunRecord`."""

    run = models.ForeignKey(
        EvalRunRecord,
        on_delete=models.CASCADE,
        related_name="scenario_results",
    )
    scenario_name = models.CharField(max_length=128)
    trial = models.IntegerField(default=0)
    verdict = models.CharField(max_length=8, choices=EvalVerdict.choices)
    terminal_reason = models.CharField(max_length=128, blank=True, default="")
    is_error = models.BooleanField(default=False)
    tool_calls = models.JSONField(default=list, blank=True)
    matcher_details = models.JSONField(default=list, blank=True)

    objects: ClassVar[EvalScenarioResultManager] = EvalScenarioResultManager()  # type: ignore[valid-type]

    class Meta:
        db_table = "teatree_eval_scenario_result"
        ordering: ClassVar = ["scenario_name", "trial"]
        indexes: ClassVar = [
            models.Index(fields=["run", "scenario_name"], name="eval_sr_run_scenario_idx"),
        ]

    def __str__(self) -> str:
        return f"eval-result<{self.run_id}:{self.scenario_name}#{self.trial}={self.verdict}>"  # ty: ignore[unresolved-attribute]
