"""IMPLEMENT — synthetic ticket + schedule_coding, riding the maker pipeline (T4-PR-3)."""

from django.test import TestCase

from teatree.core.models import DeferredQuestion, FactoryScoreSnapshot, OuterLoopExperiment, ProposalSpec, Ticket
from teatree.loops.outer_loop.implement import schedule_experiment_fix


class TestScheduleExperimentFix(TestCase):
    def _admitted(self) -> OuterLoopExperiment:
        exp = _make_experiment(
            hypothesis="Improve the review gate.",
            target_provider_id="review_catch",
            source=OuterLoopExperiment.Source.SIGNAL_REGRESSION,
        )
        question = DeferredQuestion.record("Ratify?")
        DeferredQuestion.consume(question.pk, answer="approve")
        exp.attach_ratification(DeferredQuestion.objects.get(pk=question.pk))
        exp.admit()
        return exp

    def test_creates_a_synthetic_ticket_and_transitions(self) -> None:
        exp = self._admitted()
        schedule_experiment_fix(exp)
        reloaded = OuterLoopExperiment.objects.get(pk=exp.pk)
        assert reloaded.state == OuterLoopExperiment.State.IMPLEMENTING
        assert reloaded.ticket is not None
        ticket = Ticket.objects.get(pk=reloaded.ticket_id)
        assert f"outer-loop-experiment={exp.pk}" in ticket.issue_url
        assert ticket.extra["outer_loop_experiment_id"] == exp.pk

    def test_is_idempotent_on_the_synthetic_issue_url(self) -> None:
        exp = self._admitted()
        schedule_experiment_fix(exp)
        before = Ticket.objects.count()
        # Re-driving the same experiment must not create a second ticket (the
        # unique synthetic issue URL dedups) — begin_implementation guards re-entry.
        exp2 = OuterLoopExperiment.objects.get(pk=exp.pk)
        exp2.state = OuterLoopExperiment.State.ADMITTED
        exp2.save(update_fields=["state"])
        schedule_experiment_fix(exp2)
        assert Ticket.objects.count() == before


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
