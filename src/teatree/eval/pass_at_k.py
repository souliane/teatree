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

from teatree.eval.models import EvalSpec
from teatree.eval.report import ScenarioResult

TrialRunner = Callable[[EvalSpec], ScenarioResult]


@dataclasses.dataclass(frozen=True)
class PassAtKResult:
    spec_name: str
    trials: int
    passes: int
    require: str
    skipped: bool

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
    for _ in range(k):
        result = runner(spec)
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
    )
