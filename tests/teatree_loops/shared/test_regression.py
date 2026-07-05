"""The shared anti-Goodhart collateral-regression fold (north-star PR-1 extraction).

Pure table tests over constructed :class:`FactoryScore` snapshots (no DB): a signal
newly RED/REGRESSING vs the admission baseline is the finding; a signal already worse
in the baseline that stays worse is not a NEW regression; ``exclude_provider_id`` skips
the outer loop's own target signal. This is the ONE implementation both the outer
loop's ``decide_keep`` and the future directive loop's VERIFYING step read.
"""

from teatree.core.factory_score import FactoryScore, ScoredSignal
from teatree.loops.shared.regression import no_collateral_regression


def _sig(provider_id: str, *, normalized: float | None = 0.7, verdict: str = "ok") -> ScoredSignal:
    return ScoredSignal(
        provider_id=provider_id,
        status="ok" if normalized is not None else "instrumentation_gap",
        value=normalized,
        normalized=normalized,
        weight=0.2,
        covered=normalized is not None,
        red=verdict == "red",
        verdict=verdict,
    )


def _score(signals: list[ScoredSignal]) -> FactoryScore:
    return FactoryScore(
        aggregate=0.7,
        verdict="ok",
        coverage=1.0,
        coverage_floor=0.6,
        recipe_sha="sha",
        recipe_approved=True,
        window_days=7,
        signals=signals,
    )


class TestNoCollateralRegression:
    def test_no_signal_turned_worse_is_none(self) -> None:
        baseline = _score([_sig("review_catch"), _sig("first_try_green")])
        post = _score([_sig("review_catch"), _sig("first_try_green")])
        assert no_collateral_regression(baseline, post) is None

    def test_a_signal_newly_red_is_the_finding(self) -> None:
        baseline = _score([_sig("review_catch"), _sig("first_try_green")])
        post = _score([_sig("review_catch"), _sig("first_try_green", verdict="red")])
        assert no_collateral_regression(baseline, post) == "first_try_green"

    def test_a_signal_newly_regressing_is_the_finding(self) -> None:
        baseline = _score([_sig("merge_latency")])
        post = _score([_sig("merge_latency", verdict="regressing")])
        assert no_collateral_regression(baseline, post) == "merge_latency"

    def test_a_preexisting_regression_that_stays_is_not_new(self) -> None:
        baseline = _score([_sig("merge_latency", verdict="regressing")])
        post = _score([_sig("merge_latency", verdict="regressing")])
        assert no_collateral_regression(baseline, post) is None

    def test_exclude_provider_id_skips_the_target_signal(self) -> None:
        # The excluded target may itself be RED (its improvement is judged elsewhere) —
        # excluding it is what keeps the outer loop's decide_keep behaviour intact.
        baseline = _score([_sig("review_catch")])
        post = _score([_sig("review_catch", verdict="red")])
        assert no_collateral_regression(baseline, post, exclude_provider_id="review_catch") is None

    def test_without_exclude_the_same_signal_is_the_finding(self) -> None:
        baseline = _score([_sig("review_catch")])
        post = _score([_sig("review_catch", verdict="red")])
        assert no_collateral_regression(baseline, post) == "review_catch"
