"""Durable run-history and baseline ledger for the behavioral eval harness.

Every ``t3 eval run`` invocation is recorded as one :class:`EvalRunRecord`
(the run group) plus one :class:`EvalScenarioResult` per scenario (per trial,
or one aggregate row carrying ``trials``/``score`` for a pass@k run). The run
group carries the ``git_sha`` provenance shared by all its rows.

Each scenario result stores both signals the runner produces: the
*trajectory* signal (the captured tool calls) and the *side-effect* signal
(the terminal reason + error flag). Grading verdict, per-matcher detail, the
``model`` that produced it (so a model-matrix run is reconstructable per cell),
the pass@k ``score``/``trials``, and any LLM-judge rationale are stored
alongside so a historical run is fully reconstructable without re-invoking the
model.

Fat Model: the regression-diff, pass-rate aggregation, and baseline lookup the
model-regression mode reads are queryset/manager methods here, not in callers.
``EvalRunRecord.regression_diff`` is the baseline-vs-candidate diff the
model-regression deliverable builds on; ``EvalScenarioResult`` pass-rates are
score-weighted so a single-trial verdict and a pass@k aggregate aggregate the
same way.
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
    model: str = ""

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0


@dataclasses.dataclass(frozen=True)
class ScenarioRegression:
    scenario_name: str
    baseline_pass_rate: float
    candidate_pass_rate: float
    model: str = ""

    @property
    def delta(self) -> float:
        return self.candidate_pass_rate - self.baseline_pass_rate

    @property
    def regressed(self) -> bool:
        return self.candidate_pass_rate < self.baseline_pass_rate

    @property
    def improved(self) -> bool:
        return self.candidate_pass_rate > self.baseline_pass_rate


@dataclasses.dataclass(frozen=True)
class CostRegression:
    """Per-scenario baseline-vs-candidate cost drift.

    The cost counterpart of :class:`ScenarioRegression`. ``delta`` is the
    absolute USD change; ``pct_increase`` is the *relative* drift (``delta /
    baseline``) and is ``None`` when the baseline cost is ``0.0`` (a
    subscription/free baseline carries no metered cost, so a relative increase
    is undefined — the gate no-ops that scenario rather than dividing by zero).
    """

    scenario_name: str
    baseline_cost_usd: float
    candidate_cost_usd: float
    model: str = ""

    @property
    def delta(self) -> float:
        return self.candidate_cost_usd - self.baseline_cost_usd

    @property
    def pct_increase(self) -> float | None:
        # Cost is non-negative; a 0.0 baseline means no metered reference (a
        # subscription/free baseline), so the relative drift is undefined.
        if self.baseline_cost_usd <= 0.0:
            return None
        return self.delta / self.baseline_cost_usd


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

    # ast-grep-ignore: ac-django-no-complexity-suppressions
    def record(  # noqa: PLR0913 — run-ledger create API; each kwarg is a documented run attribute.
        self,
        *,
        model: str,
        suite: str = "",
        overlay: str = "",
        max_turns_override: int | None = None,
        is_baseline: bool = False,
        git_sha: str = "",
    ) -> "EvalRunRecord":
        return self.create(
            model=model,
            suite=suite,
            overlay=overlay,
            max_turns_override=max_turns_override,
            is_baseline=is_baseline,
            git_sha=git_sha,
        )


class EvalRunRecord(models.Model):
    """One ``t3 eval run`` invocation across all selected scenarios."""

    started_at = models.DateTimeField(default=timezone.now)
    model = models.CharField(max_length=128)
    suite = models.CharField(max_length=128, blank=True, default="")
    overlay = models.CharField(max_length=64, blank=True, default="")
    max_turns_override = models.IntegerField(null=True, blank=True, default=None)
    is_baseline = models.BooleanField(default=False)
    git_sha = models.CharField(max_length=64, blank=True, default="")

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

    @property
    def models(self) -> list[str]:
        seen = {r for r in self.results.values_list("model", flat=True) if r}
        return sorted(seen) if seen else [self.model]

    def mark_baseline(self) -> None:
        type(self).objects.for_model(self.model).baselines().exclude(pk=self.pk).update(is_baseline=False)
        self.is_baseline = True
        self.save(update_fields=["is_baseline"])

    # ast-grep-ignore: ac-django-no-complexity-suppressions
    def record_scenario(  # noqa: PLR0913 — per-scenario ledger record API; each kwarg is a documented field.
        self,
        *,
        scenario_name: str,
        verdict: str,
        trial: int = 0,
        model: str = "",
        score: float | None = None,
        trials: int = 1,
        terminal_reason: str = "",
        is_error: bool = False,
        tool_calls: list[TrajectoryToolCall] | None = None,
        matcher_details: list[MatcherDetail] | None = None,
        judge_rationale: str = "",
        cost_usd: float = 0.0,
        main_cost_usd: float = 0.0,
        aux_cost_usd: float = 0.0,
        input_tokens: int | None = None,
        cache_creation_tokens: int | None = None,
        cache_read_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> "EvalScenarioResult":
        return EvalScenarioResult.objects.create(
            run=self,
            scenario_name=scenario_name,
            trial=trial,
            model=model,
            verdict=verdict,
            score=_default_score(verdict) if score is None else score,
            trials=trials,
            terminal_reason=terminal_reason,
            is_error=is_error,
            tool_calls=tool_calls or [],
            matcher_details=matcher_details or [],
            judge_rationale=judge_rationale,
            cost_usd=cost_usd,
            main_cost_usd=main_cost_usd,
            aux_cost_usd=aux_cost_usd,
            input_tokens=input_tokens,
            cache_creation_tokens=cache_creation_tokens,
            cache_read_tokens=cache_read_tokens,
            output_tokens=output_tokens,
        )

    def pass_rates(self, *, model: str | None = None) -> list[ScenarioPassRate]:
        results = self.results if model is None else self.results.filter(model=model)
        return results.pass_rates()

    @classmethod
    def regression_diff(
        cls,
        *,
        baseline: "EvalRunRecord",
        candidate: "EvalRunRecord",
        model: str | None = None,
    ) -> list[ScenarioRegression]:
        """Diff candidate against baseline per scenario.

        ``model`` restricts both sides to one model's rows — used when gating a
        model-matrix candidate (whose run holds several models' rows) against a
        single-model baseline. Omit it for a normal single-model run.
        """
        baseline_rates = _rates_by_scenario(baseline.pass_rates(model=model))
        candidate_rates = _rates_by_scenario(candidate.pass_rates(model=model))
        scenarios = sorted(set(baseline_rates) | set(candidate_rates))
        return [
            ScenarioRegression(
                scenario_name=name,
                model=model or "",
                baseline_pass_rate=baseline_rates.get(name, 0.0),
                candidate_pass_rate=candidate_rates.get(name, 0.0),
            )
            for name in scenarios
        ]

    @classmethod
    def cost_regression_diff(
        cls,
        *,
        baseline: "EvalRunRecord",
        candidate: "EvalRunRecord",
        model: str | None = None,
    ) -> list[CostRegression]:
        """Diff candidate against baseline per-scenario ``cost_usd``.

        Mirrors :meth:`regression_diff` on the cost signal: the baseline run's
        per-scenario cost is the reference (the same ``is_baseline`` run drives
        both score and cost gating). ``model`` restricts both sides to one
        model's rows for a model-matrix candidate. A scenario present on only
        one side defaults the missing cost to ``0.0``.
        """
        baseline_costs = _costs_by_scenario(baseline, model=model)
        candidate_costs = _costs_by_scenario(candidate, model=model)
        scenarios = sorted(set(baseline_costs) | set(candidate_costs))
        return [
            CostRegression(
                scenario_name=name,
                model=model or "",
                baseline_cost_usd=baseline_costs.get(name, 0.0),
                candidate_cost_usd=candidate_costs.get(name, 0.0),
            )
            for name in scenarios
        ]

    def costs_by_scenario(self, *, model: str | None = None) -> dict[str, float]:
        """Per-scenario total ``cost_usd`` for this run, optionally filtered to one model.

        The recorded-cost map the declarative cost-bounds gate
        (:func:`teatree.eval.cost_bounds.check_cost_bounds`) checks against its
        checked-in ceilings. Sums across rows sharing a scenario name, the same
        way :meth:`cost_regression_diff` aggregates its sides.
        """
        return _costs_by_scenario(self, model=model)


def _costs_by_scenario(run: "EvalRunRecord", *, model: str | None) -> dict[str, float]:
    """Per-scenario total ``cost_usd`` for *run*, optionally filtered to one model.

    Sums across rows sharing a scenario name (a pass@k aggregate is one row; a
    model-matrix scenario spans one row per model) so a scenario compares as a
    single number, matching :func:`_rates_by_scenario`.
    """
    rows = run.results if model is None else run.results.filter(model=model)
    costs: dict[str, float] = {}
    for name, cost in rows.values_list("scenario_name", "cost_usd"):
        costs[name] = costs.get(name, 0.0) + cost
    return costs


def _default_score(verdict: str) -> float:
    return 1.0 if verdict == EvalVerdict.PASS else 0.0


def _rates_by_scenario(rates: list[ScenarioPassRate]) -> dict[str, float]:
    """Collapse per-(scenario, model) pass-rates to one rate per scenario.

    A single-model run (or a model-filtered diff) has one entry per scenario,
    so this is a pass-through; a mixed-model run averages the per-model rates so
    a scenario still compares as a single number.
    """
    grouped: dict[str, list[float]] = {}
    for rate in rates:
        grouped.setdefault(rate.scenario_name, []).append(rate.pass_rate)
    return {name: sum(values) / len(values) for name, values in grouped.items()}


class EvalScenarioResultQuerySet(models.QuerySet["EvalScenarioResult"]):
    def graded(self) -> "EvalScenarioResultQuerySet":
        return self.exclude(verdict=EvalVerdict.SKIP)

    def pass_rates(self) -> list[ScenarioPassRate]:
        scores: dict[tuple[str, str], list[float]] = {}
        for name, model, score in self.graded().values_list("scenario_name", "model", "score"):
            scores.setdefault((name, model), []).append(score)
        rates: list[ScenarioPassRate] = []
        for (name, model), values in sorted(scores.items()):
            total = len(values)
            passed = round(sum(values))
            rates.append(ScenarioPassRate(scenario_name=name, model=model, total=total, passed=passed))
        return rates


EvalScenarioResultManager = models.Manager.from_queryset(EvalScenarioResultQuerySet)


class EvalScenarioResult(models.Model):
    """One scenario's grading verdict for one model within an :class:`EvalRunRecord`.

    A single-trial run records one row per scenario with ``trials=1`` and
    ``score`` 1.0/0.0. A pass@k run records one aggregate row with ``trials=k``
    and ``score`` the pass-rate. A model-matrix run records one row per
    ``(scenario, model)`` cell. ``pass_rates`` is score-weighted so all three
    aggregate identically.
    """

    run = models.ForeignKey(
        EvalRunRecord,
        on_delete=models.CASCADE,
        related_name="scenario_results",
    )
    scenario_name = models.CharField(max_length=128)
    trial = models.IntegerField(default=0)
    model = models.CharField(max_length=64, blank=True, default="")
    verdict = models.CharField(max_length=8, choices=EvalVerdict)
    score = models.FloatField(default=0.0)
    trials = models.PositiveSmallIntegerField(default=1)
    terminal_reason = models.CharField(max_length=128, blank=True, default="")
    is_error = models.BooleanField(default=False)
    tool_calls = models.JSONField(default=list, blank=True)
    matcher_details = models.JSONField(default=list, blank=True)
    judge_rationale = models.CharField(max_length=512, blank=True, default="")
    cost_usd = models.FloatField(default=0.0)
    # Metered cost split: the requested MAIN model vs the AUXILIARY background
    # (Claude Code's claude-haiku-4-5), from per-model model_usage.costUSD. 0.0 on
    # a non-metered/subscription row (cost_usd is also 0 there, so 0 is unambiguous).
    main_cost_usd = models.FloatField(default=0.0)
    aux_cost_usd = models.FloatField(default=0.0)
    # Token usage split by cache class. NULLABLE so NULL (a legacy / subscription
    # / offline row with no usage signal) is distinct from a real metered 0.
    input_tokens = models.IntegerField(null=True, blank=True, default=None)
    cache_creation_tokens = models.IntegerField(null=True, blank=True, default=None)
    cache_read_tokens = models.IntegerField(null=True, blank=True, default=None)
    output_tokens = models.IntegerField(null=True, blank=True, default=None)

    objects = EvalScenarioResultManager()

    class Meta:
        db_table = "teatree_eval_scenario_result"
        ordering: ClassVar = ["scenario_name", "model", "trial"]
        indexes: ClassVar = [
            models.Index(fields=["run", "scenario_name"], name="eval_sr_run_scenario_idx"),
            models.Index(fields=["run", "model"], name="eval_sr_run_model_idx"),
        ]

    def __str__(self) -> str:
        return f"eval-result<{self.run_id}:{self.scenario_name}#{self.trial}={self.verdict}>"  # ty: ignore[unresolved-attribute]
