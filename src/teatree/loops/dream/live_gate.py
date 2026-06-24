"""The live-model pass@k gate that the dreaming promotion ladder runs before a write (#2634).

The anti-vacuity guard (:func:`teatree.loops.dream.promote.guard_can_fail`) proves only
that a candidate's grader CAN fail a synthetic bad transcript — never that the scenario
actually PASSES against a real model. Two of three auto-promoted scenarios failed a live
pass@3 on a mismatched templated grader, so promotion now also requires a live-model
pass@k confirmed through this gate.

The validation seam is injectable: :func:`build_live_validator` is the real, METERED
implementation, while tests inject a fake so no real model ever runs in the suite. A
``None`` validator means the metered check is NOT run — the safety property that
withholds every candidate from the gating suite (the nightly ``tick`` path).
"""

from dataclasses import dataclass
from typing import Protocol

from teatree.eval.models import EvalSpec
from teatree.loops.dream.promotion_outcome import PromotionOutcome

#: pass@k defaults matching the CI ``eval.yml`` semantics — a candidate must pass
#: at least one of ``DEFAULT_LIVE_TRIALS`` live trials (pass@k, ``require="any"``).
DEFAULT_LIVE_TRIALS = 3
DEFAULT_LIVE_REQUIRE = "any"


class LiveValidator(Protocol):
    """Runs a candidate's would-be scenario against a LIVE model and returns the pass@k verdict.

    The injectable seam that gates promotion on a live-model pass (the soundness
    fix for #2634-class drift): the anti-vacuity guard only proves the grader has
    teeth against SYNTHETIC fixtures, never that the scenario actually PASSES
    against a real model. Two of three auto-promoted scenarios failed a live
    pass@3 because the one-size templated grader did not fit the rule, so a
    scenario now lands ONLY when a ``LiveValidator`` confirms a live pass@k.

    Passes the candidate's spec through a real runner *trials* times and returns
    the pass@k verdict (``require``-of-``trials``). The production implementation
    (:func:`build_live_validator`) is METERED; tests inject a fake so no real
    model ever runs in the suite.
    """

    def __call__(self, spec: EvalSpec, *, trials: int, require: str) -> bool: ...


def build_live_validator() -> LiveValidator:
    """The real, METERED live validator: run the candidate's spec via the SDK runner pass@k.

    Wraps :class:`~teatree.eval.api_runner.ApiInProcessRunner` (subscription-metered,
    ``require_executed`` so a missing ``claude`` fails loud rather than decoratively
    skipping a gate the promotion depends on) and aggregates via
    :func:`~teatree.eval.pass_at_k.run_pass_at_k`. Returns the gate verdict
    (:attr:`PassAtKResult.ok`): a candidate passes only when the live pass@k holds.

    This is the OPT-IN path — ``t3 dream run --full`` supplies it; the nightly
    ``tick`` does NOT (it withholds, so nothing auto-lands without a metered check).
    """
    from teatree.eval.api_runner import ApiInProcessRunner  # noqa: PLC0415
    from teatree.eval.pass_at_k import run_pass_at_k  # noqa: PLC0415
    from teatree.eval.report import evaluate as evaluate_run  # noqa: PLC0415

    def _validate(spec: EvalSpec, *, trials: int, require: str) -> bool:
        runner = ApiInProcessRunner(require_executed=True)
        return run_pass_at_k(spec, lambda s: evaluate_run(s, runner.run(s)), k=trials, require=require).ok

    return _validate


@dataclass(frozen=True, slots=True)
class LiveGate:
    """The live-model pass@k gate config threaded into promotion.

    Bundles the injectable *validator* with its pass@k knobs (*trials* / *require*,
    defaulting to the CI ``eval.yml`` semantics) so the single live-validation
    concern travels as ONE cohesive value rather than three loose parameters. A
    ``None`` validator means the metered check is NOT run — the safety property
    that withholds every candidate from the gating suite.
    """

    validator: LiveValidator | None = None
    trials: int = DEFAULT_LIVE_TRIALS
    require: str = DEFAULT_LIVE_REQUIRE

    def verdict(self, spec: EvalSpec) -> PromotionOutcome | None:
        """The withholding outcome when the live gate does not pass, else ``None``.

        ``None`` validator → retryable withhold ("validation not run", the safety
        property). A run that FAILs pass@k → a terminal-rejected withhold. A PASS →
        ``None`` (the gate is clear and promotion proceeds).
        """
        name = spec.name
        if self.validator is None:
            return PromotionOutcome(
                scenario_name=name, promoted=False, reason="withheld: live-model validation not run", retryable=True
            )
        if not self.validator(spec, trials=self.trials, require=self.require):
            return PromotionOutcome(
                scenario_name=name, promoted=False, reason=f"withheld: failed live-model pass@{self.trials}"
            )
        return None
