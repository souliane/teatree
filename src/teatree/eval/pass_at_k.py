"""pass@k aggregation for behavioral eval scenarios.

The base harness runs each scenario once. A single trial against an LLM is
noisy: a flaky-but-mostly-right agent can go red on one unlucky sample, and a
mostly-wrong agent can go green on one lucky sample. pass@k re-runs a scenario
``k`` times and aggregates, so flake-resistance is observable rather than
assumed.

Two aggregation modes:

*   ``pass@k`` (``require="any"``) — the scenario counts as passing if **any**
    of the ``k`` trials passed. Use for "is the agent *capable* of the right
    behavior" framing.
*   ``pass^k`` / all-of (``require="all"``) — passing requires **every** trial
    to pass. Use for a regression gate where intermittent compliance is itself
    a failure.

The runner is injected (any callable mapping ``EvalSpec -> ScenarioResult``),
so tests drive it with a deterministic stub and production passes a closure
over :class:`~teatree.eval.sdk_runner.SdkInProcessRunner` + ``evaluate``.
"""

import dataclasses
from collections.abc import Callable

from teatree.eval.models import CAP_TERMINAL_REASONS, EvalSpec, TokenUsage
from teatree.eval.report import ScenarioResult

TrialRunner = Callable[[EvalSpec], ScenarioResult]


@dataclasses.dataclass(frozen=True)
class PassAtKResult:
    spec_name: str
    trials: int
    passes: int
    require: str
    skipped: bool
    #: Total metered cost across every trial (0.0 for a non-metered/subscription
    #: run) — the substrate the cost-regression gate reads in the pass@k lane.
    cost_usd: float = 0.0
    #: Total token usage summed across every trial (all-zero for a non-metered
    #: run), mirroring ``cost_usd`` — the substrate for the benchmark's cache
    #: columns when a cell runs k trials.
    usage: TokenUsage = dataclasses.field(default_factory=TokenUsage)
    #: The billed model of the LAST trial (the model that actually ran;
    #: ``None`` for a non-metered run) — diagnostics only, NOT the fallback signal.
    billed_model: str | None = None
    #: A cap reason (from :data:`~teatree.eval.models.CAP_TERMINAL_REASONS`) if
    #: ANY trial was cap-truncated, else ``""``. Because ``cost_usd``/``usage``
    #: are SUMMED across trials, the aggregated cell's billed identity holds only
    #: when EVERY trial finished cleanly — one capped trial taints the sum. The
    #: benchmark threads this onto ``MatrixRow.terminal_reason`` so a multi-trial
    #: cell with a capped trial is excluded from the warm-equivalent fit exactly
    #: like the single-trial path.
    terminal_reason: str = ""
    #: Whether ANY trial substituted the requested main model (a fallback).
    #: ``True`` if any observed trial fell back; ``False`` if every observed trial
    #: kept the requested model; ``None`` when no trial was observable
    #: (subscription/offline). The benchmark threads it onto ``MatrixRow.fell_back``.
    fell_back: bool | None = None
    #: MAIN-model and AUXILIARY (haiku background) cost summed across every trial
    #: (``0.0`` for a non-metered run) — the per-variant main/aux cost split.
    main_cost_usd: float = 0.0
    aux_cost_usd: float = 0.0
    #: MAIN-model and AUXILIARY token usage summed across every trial.
    main_usage: TokenUsage = dataclasses.field(default_factory=TokenUsage)
    aux_usage: TokenUsage = dataclasses.field(default_factory=TokenUsage)

    @property
    def pass_rate(self) -> float:
        return self.passes / self.trials if self.trials else 0.0

    @property
    def ok(self) -> bool:
        if self.skipped:
            return True
        if self.require == "all":
            return self.passes == self.trials
        return self.passes >= 1


def run_pass_at_k(
    spec: EvalSpec,
    runner: TrialRunner,
    *,
    k: int,
    require: str = "any",
) -> PassAtKResult:
    if k < 1:
        msg = f"k must be >= 1, got {k}"
        raise ValueError(msg)
    if require not in {"any", "all"}:
        msg = f"require must be 'any' or 'all', got {require!r}"
        raise ValueError(msg)
    passes = 0
    skipped_all = True
    cost_usd = 0.0
    usage = TokenUsage()
    main_cost_usd = aux_cost_usd = 0.0
    main_usage = aux_usage = TokenUsage()
    billed_model: str | None = None
    cap_reason = ""
    fell_back: bool | None = None
    for _ in range(k):
        result = runner(spec)
        cost_usd += result.run.cost_usd
        usage += result.run.usage
        main_cost_usd += result.run.main_cost_usd
        aux_cost_usd += result.run.aux_cost_usd
        main_usage += result.run.main_usage
        aux_usage += result.run.aux_usage
        if result.run.billed_model is not None:
            billed_model = result.run.billed_model
        fell_back = _fold_fell_back(aggregate=fell_back, trial=result.run.fell_back)
        if not cap_reason and result.run.terminal_reason in CAP_TERMINAL_REASONS:
            cap_reason = result.run.terminal_reason
        if result.skipped:
            continue
        skipped_all = False
        if result.passed:
            passes += 1
    return PassAtKResult(
        spec_name=spec.name,
        trials=k,
        passes=passes,
        require=require,
        skipped=skipped_all,
        cost_usd=cost_usd,
        usage=usage,
        billed_model=billed_model,
        terminal_reason=cap_reason,
        fell_back=fell_back,
        main_cost_usd=main_cost_usd,
        aux_cost_usd=aux_cost_usd,
        main_usage=main_usage,
        aux_usage=aux_usage,
    )


def _fold_fell_back(*, aggregate: bool | None, trial: bool | None) -> bool | None:
    """Fold one trial's fallback signal into the aggregate (any-observed-fallback wins).

    ``True`` if ANY observed trial fell back; ``False`` if at least one trial was
    observable and none fell back; ``None`` only while no trial has been
    observable (every trial subscription/offline).
    """
    if trial is None:
        return aggregate
    if aggregate is None:
        return trial
    return aggregate or trial
