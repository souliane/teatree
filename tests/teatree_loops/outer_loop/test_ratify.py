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

    def _answered(self, answer: str) -> OuterLoopExperiment:
        exp = self._proposed()
        question = DeferredQuestion.record(f"Ratify experiment {exp.pk}?", options_hash=f"outer_loop_ratify:{exp.pk}")
        exp.attach_ratification(question)
        DeferredQuestion.consume(question.pk, answer=answer)
        return self._fresh(exp)

    def test_ask_records_and_transitions(self) -> None:
        exp = self._proposed()
        question = ask_ratification(exp)
        assert exp.state == OuterLoopExperiment.State.RATIFY_PENDING
        assert exp.ratify_question_id == question.pk

    def test_the_standing_answer_admits_at_once(self) -> None:
        # An undecided question is internal: the standing "approve" answers the ask by policy, so nothing waits.
        exp = self._proposed()
        question = ask_ratification(exp)
        assert DeferredQuestion.objects.get(pk=question.pk).resolved_via == DeferredQuestion.ResolvedVia.POLICY
        assert try_admit(self._fresh(exp)) == "admitted"

    def test_admits_on_approval(self) -> None:
        exp = self._answered("approve")
        assert try_admit(exp) == "admitted"
        assert self._fresh(exp).state == OuterLoopExperiment.State.ADMITTED

    def test_rejects_on_denial(self) -> None:
        exp = self._answered("no")
        assert try_admit(exp) == "rejected"
        assert self._fresh(exp).state == OuterLoopExperiment.State.REJECTED

    def test_a_prose_approval_admits(self) -> None:
        # The exact-token match this loop used to carry rejected every one of these.
        for answer in ("RATIFIED, NO SETTING — just do it.", "Approved. Ship it."):
            exp = self._answered(answer)
            assert try_admit(exp) == "admitted", answer
            assert self._fresh(exp).state == OuterLoopExperiment.State.ADMITTED, answer

    def test_an_undecidable_answer_re_asks_instead_of_reaching_the_terminal_state(self) -> None:
        for answer in ("not approved yet", "let's talk about this at standup tomorrow"):
            exp = self._answered(answer)
            first = exp.ratify_question
            assert try_admit(exp) == "reasked", answer
            held = self._fresh(exp)
            assert held.state == OuterLoopExperiment.State.RATIFY_PENDING, answer
            assert held.ratify_question is not None
            assert held.ratify_question.pk != first.pk, answer
            assert held.ratify_question.answered_at is None, answer
            assert try_admit(held) == "pending", answer


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
