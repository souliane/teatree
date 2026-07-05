"""RATIFY — the only ADMITTED writer, gated on a consumed answer (T4-PR-3)."""

from django.test import TestCase

from teatree.core.models import DeferredQuestion, FactoryScoreSnapshot, OuterLoopExperiment, ProposalSpec
from teatree.loops.outer_loop.ratify import ask_ratification, try_admit


class TestRatify(TestCase):
    def _proposed(self) -> OuterLoopExperiment:
        return _make_experiment(
            hypothesis="H", target_provider_id="review_catch", source=OuterLoopExperiment.Source.SIGNAL_REGRESSION
        )

    def _fresh(self, exp: OuterLoopExperiment) -> OuterLoopExperiment:
        return OuterLoopExperiment.objects.get(pk=exp.pk)

    def test_ask_records_and_transitions(self) -> None:
        exp = self._proposed()
        question = ask_ratification(exp)
        assert exp.state == OuterLoopExperiment.State.RATIFY_PENDING
        assert exp.ratify_question_id == question.pk

    def test_pending_until_answered(self) -> None:
        exp = self._proposed()
        ask_ratification(exp)
        assert try_admit(self._fresh(exp)) == "pending"

    def test_admits_on_approval(self) -> None:
        exp = self._proposed()
        question = ask_ratification(exp)
        DeferredQuestion.consume(question.pk, answer="approve")
        assert try_admit(self._fresh(exp)) == "admitted"
        assert self._fresh(exp).state == OuterLoopExperiment.State.ADMITTED

    def test_rejects_on_denial(self) -> None:
        exp = self._proposed()
        question = ask_ratification(exp)
        DeferredQuestion.consume(question.pk, answer="no")
        assert try_admit(self._fresh(exp)) == "rejected"
        assert self._fresh(exp).state == OuterLoopExperiment.State.REJECTED


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
