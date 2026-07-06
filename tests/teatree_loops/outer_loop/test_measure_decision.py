"""The keep-only-if-better decision rule — the anti-Goodhart core (T4-PR-3).

Pure table tests over constructed :class:`FactoryScore` snapshots (no DB): a
target that improved beyond its band with no collateral regression is KEPT;
every non-improving, noise-band, or collateral-damage case is REVERTED. This is
the invariant the whole outer loop exists to protect — a non-improving
experiment must never be kept.
"""

from teatree.core.factory.factory_score import FactoryScore, ScoredSignal
from teatree.loops.outer_loop.decide import decide_keep


def _sig(provider_id: str, *, normalized: float | None, verdict: str = "ok") -> ScoredSignal:
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


def _score(signals: list[ScoredSignal], *, verdict: str = "ok", aggregate: float | None = 0.7) -> FactoryScore:
    return FactoryScore(
        aggregate=aggregate,
        verdict=verdict,
        coverage=1.0,
        coverage_floor=0.6,
        recipe_sha="sha",
        recipe_approved=True,
        window_days=7,
        signals=signals,
    )


class TestDecideKeep:
    def test_target_improved_no_regression_is_kept(self) -> None:
        baseline = _score([_sig("review_catch", normalized=0.50), _sig("first_try_green", normalized=0.80)])
        post = _score([_sig("review_catch", normalized=0.70), _sig("first_try_green", normalized=0.80)])
        decision = decide_keep(baseline=baseline, post=post, target_provider_id="review_catch", regress_band=0.05)
        assert decision.keep is True

    def test_target_not_improved_is_reverted(self) -> None:
        baseline = _score([_sig("review_catch", normalized=0.50), _sig("first_try_green", normalized=0.80)])
        post = _score([_sig("review_catch", normalized=0.51), _sig("first_try_green", normalized=0.80)])
        decision = decide_keep(baseline=baseline, post=post, target_provider_id="review_catch", regress_band=0.05)
        assert decision.keep is False

    def test_improvement_inside_band_is_noise_not_kept(self) -> None:
        baseline = _score([_sig("review_catch", normalized=0.50)])
        post = _score([_sig("review_catch", normalized=0.54)])
        decision = decide_keep(baseline=baseline, post=post, target_provider_id="review_catch", regress_band=0.05)
        assert decision.keep is False

    def test_collateral_regression_blocks_a_kept_target(self) -> None:
        # Target improved, but another signal turned RED vs a healthy baseline —
        # the no-regression-anywhere rule reverts even a real target win.
        baseline = _score([_sig("review_catch", normalized=0.50), _sig("first_try_green", normalized=0.80)])
        post = _score([_sig("review_catch", normalized=0.75), _sig("first_try_green", normalized=0.30, verdict="red")])
        decision = decide_keep(baseline=baseline, post=post, target_provider_id="review_catch", regress_band=0.05)
        assert decision.keep is False
        assert "first_try_green" in decision.reason

    def test_preexisting_regression_does_not_block(self) -> None:
        # A signal already regressing in the baseline that STAYS regressing is not
        # a NEW collateral regression — it must not block a genuine target win.
        baseline = _score(
            [_sig("review_catch", normalized=0.50), _sig("merge_latency", normalized=0.40, verdict="regressing")]
        )
        post = _score(
            [_sig("review_catch", normalized=0.75), _sig("merge_latency", normalized=0.40, verdict="regressing")]
        )
        decision = decide_keep(baseline=baseline, post=post, target_provider_id="review_catch", regress_band=0.05)
        assert decision.keep is True

    def test_red_post_score_is_never_kept(self) -> None:
        baseline = _score([_sig("review_catch", normalized=0.50)])
        post = _score([_sig("review_catch", normalized=0.90)], verdict="red", aggregate=None)
        decision = decide_keep(baseline=baseline, post=post, target_provider_id="review_catch", regress_band=0.05)
        assert decision.keep is False

    def test_uncovered_target_is_never_kept(self) -> None:
        baseline = _score([_sig("review_catch", normalized=None, verdict="instrumentation_gap")])
        post = _score([_sig("review_catch", normalized=0.90)])
        decision = decide_keep(baseline=baseline, post=post, target_provider_id="review_catch", regress_band=0.05)
        assert decision.keep is False

    def test_missing_target_is_never_kept(self) -> None:
        baseline = _score([_sig("first_try_green", normalized=0.80)])
        post = _score([_sig("first_try_green", normalized=0.90)])
        decision = decide_keep(baseline=baseline, post=post, target_provider_id="review_catch", regress_band=0.05)
        assert decision.keep is False
