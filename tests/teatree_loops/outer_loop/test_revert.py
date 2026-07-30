"""The REVERT flow — human-ratified rollback reaches terminal REVERTED (T4-PR-3).

Proves the soft-lock is gone: a non-improving experiment ASKS the human to revert
and, on `t3 outer resolve-revert`, reaches the terminal REVERTED state — freeing
the max-concurrent=1 slot so a second experiment can start.
"""

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import DeferredQuestion, FactoryScoreSnapshot, OuterLoopExperiment, ProposalSpec, Ticket
from teatree.loops.outer_loop.revert import ask_revert, resolve_revert


def _revert_pending() -> OuterLoopExperiment:
    exp = OuterLoopExperiment.objects.propose(
        ProposalSpec(hypothesis="H", target_provider_id="review_catch", source=OuterLoopExperiment.Source.OPERATOR)
    )
    answered = DeferredQuestion.record("Ratify?")
    DeferredQuestion.consume(answered.pk, answer="approve")
    exp.attach_ratification(DeferredQuestion.objects.get(pk=answered.pk))
    exp.admit()
    exp.begin_implementation(
        Ticket.objects.create(issue_url=f"https://e.com/{timezone.now().timestamp()}", role=Ticket.Role.AUTHOR)
    )
    exp.arm_measure()
    exp.request_revert(post_snapshot=_snapshot(), reason="no improvement")
    return exp


def _snapshot() -> FactoryScoreSnapshot:
    return FactoryScoreSnapshot.objects.create(
        overlay="", window_days=7, recipe_sha="s", aggregate=0.6, verdict="ok", coverage=1.0, coverage_floor=0.6
    )


class TestRevertFlow(TestCase):
    def test_ask_records_the_revert_question(self) -> None:
        exp = _revert_pending()
        question = ask_revert(exp)
        assert exp.revert_question_id == question.pk
        assert exp.state == OuterLoopExperiment.State.REVERT_PENDING  # ask does not terminate

    def test_resolve_reaches_terminal_reverted_and_frees_the_slot(self) -> None:
        exp = _revert_pending()
        assert OuterLoopExperiment.objects.active_count() == 1  # holds the concurrency slot
        resolve_revert(exp, revert_sha="cafe")
        reloaded = OuterLoopExperiment.objects.get(pk=exp.pk)
        assert reloaded.state == OuterLoopExperiment.State.REVERTED
        assert reloaded.revert_sha == "cafe"
        assert reloaded.is_terminal
        # Slot freed → a second experiment can now start.
        assert OuterLoopExperiment.objects.active_count() == 0

    def test_resolve_asks_first_when_the_tick_has_not(self) -> None:
        # An operator resolving before the tick asked still lands REVERTED (resolve
        # creates + consumes the audit question itself).
        exp = _revert_pending()
        assert exp.revert_question is None
        resolve_revert(exp)
        assert OuterLoopExperiment.objects.get(pk=exp.pk).state == OuterLoopExperiment.State.REVERTED

    def test_resolve_skips_reconsume_when_question_already_answered(self) -> None:
        # The tick asked and the human answered the question directly; resolve then
        # only records the revert (no double-consume).
        exp = _revert_pending()
        question = ask_revert(exp)
        DeferredQuestion.consume(question.pk, answer="reverted")
        exp.revert_question = DeferredQuestion.objects.get(pk=question.pk)
        resolve_revert(exp, revert_sha="beef")
        assert OuterLoopExperiment.objects.get(pk=exp.pk).state == OuterLoopExperiment.State.REVERTED
