"""MEASURE + DECIDE integration — horizon, post-snapshot, keep/revert (T4-PR-3)."""

import datetime as dt
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from teatree.core.factory.factory_score import FactoryScore, ScoredSignal
from teatree.core.models import DeferredQuestion, FactoryScoreSnapshot, OuterLoopExperiment, ProposalSpec, Ticket
from teatree.loops.outer_loop.measure import arm_measurement, horizon_elapsed, measure_and_decide


def _sig(provider_id: str, normalized: float) -> ScoredSignal:
    return ScoredSignal(
        provider_id=provider_id,
        status="ok",
        value=normalized,
        normalized=normalized,
        weight=0.5,
        covered=True,
        red=False,
        verdict="ok",
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


def _snapshot_from(score: FactoryScore) -> FactoryScoreSnapshot:
    return FactoryScoreSnapshot.objects.record_snapshot(score, tree_sha="base", overlay="")


class TestMeasure(TestCase):
    def _measuring(self, baseline: FactoryScore | None) -> OuterLoopExperiment:
        snapshot = _snapshot_from(baseline) if baseline is not None else None
        exp = _make_experiment(
            hypothesis="H",
            target_provider_id="review_catch",
            source=OuterLoopExperiment.Source.OPERATOR,
            regress_band=0.05,
            baseline_snapshot=snapshot,
        )
        question = DeferredQuestion.record("Ratify?")
        DeferredQuestion.consume(question.pk, answer="approve")
        exp.ratify_question = DeferredQuestion.objects.get(pk=question.pk)
        exp.state = OuterLoopExperiment.State.RATIFY_PENDING
        exp.save(update_fields=["ratify_question", "state"])
        exp.admit()
        exp.begin_implementation(
            Ticket.objects.create(issue_url=f"https://e.com/{timezone.now().timestamp()}", role=Ticket.Role.AUTHOR)
        )
        exp.arm_measure()
        return exp

    def test_horizon_gate(self) -> None:
        exp = self._measuring(_score([_sig("review_catch", 0.5)]))
        started = exp.measure_started_at
        assert horizon_elapsed(exp, measure_days=7, now=started + dt.timedelta(days=8))
        assert not horizon_elapsed(exp, measure_days=7, now=started + dt.timedelta(days=1))

    def test_horizon_not_elapsed_when_clock_unarmed(self) -> None:
        exp = _make_experiment(
            hypothesis="H", target_provider_id="review_catch", source=OuterLoopExperiment.Source.OPERATOR
        )
        assert horizon_elapsed(exp, measure_days=7, now=timezone.now()) is False

    def test_measure_survives_a_raising_head_sha_probe(self) -> None:
        # The best-effort head-sha provenance never crashes a measure: a raising
        # git probe degrades to an empty tree_sha rather than failing the decision.
        exp = self._measuring(_score([_sig("review_catch", 0.50)]))
        with mock.patch("teatree.loops.outer_loop.measure.head_sha", side_effect=RuntimeError("no git")):
            decision = measure_and_decide(exp, post_score=_score([_sig("review_catch", 0.90)]))
        assert decision.keep is True

    def test_improving_experiment_requests_keep(self) -> None:
        # H1-KEEP: an improving experiment parks in KEEP_PENDING (awaiting a human's
        # keep-ratification), never auto-KEPT.
        exp = self._measuring(_score([_sig("review_catch", 0.50), _sig("first_try_green", 0.80)]))
        post = _score([_sig("review_catch", 0.80), _sig("first_try_green", 0.80)])
        decision = measure_and_decide(exp, post_score=post)
        assert decision.keep is True
        assert OuterLoopExperiment.objects.get(pk=exp.pk).state == OuterLoopExperiment.State.KEEP_PENDING

    def test_non_improving_experiment_requests_revert(self) -> None:
        exp = self._measuring(_score([_sig("review_catch", 0.50)]))
        post = _score([_sig("review_catch", 0.51)])
        decision = measure_and_decide(exp, post_score=post)
        assert decision.keep is False
        assert OuterLoopExperiment.objects.get(pk=exp.pk).state == OuterLoopExperiment.State.REVERT_PENDING

    def test_missing_baseline_is_a_conservative_revert(self) -> None:
        exp = self._measuring(None)
        post = _score([_sig("review_catch", 0.99)])
        decision = measure_and_decide(exp, post_score=post)
        assert decision.keep is False
        assert "baseline" in decision.reason

    def test_arm_measurement_helper_transitions(self) -> None:
        exp = _make_experiment(
            hypothesis="H", target_provider_id="review_catch", source=OuterLoopExperiment.Source.OPERATOR
        )
        q = DeferredQuestion.record("Q")
        DeferredQuestion.consume(q.pk, answer="approve")
        exp.attach_ratification(DeferredQuestion.objects.get(pk=q.pk))
        exp.admit()
        exp.begin_implementation(
            Ticket.objects.create(issue_url=f"https://e.com/{timezone.now().timestamp()}", role=Ticket.Role.AUTHOR)
        )
        arm_measurement(exp)
        assert exp.state == OuterLoopExperiment.State.MEASURING


def _make_experiment(
    *,
    overlay: str = "",
    baseline_snapshot: FactoryScoreSnapshot | None = None,
    **spec_kw: object,
) -> OuterLoopExperiment:
    """Build an experiment via the ProposalSpec factory (test convenience)."""
    return OuterLoopExperiment.objects.propose(
        ProposalSpec(**spec_kw), overlay=overlay, baseline_snapshot=baseline_snapshot
    )
