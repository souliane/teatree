"""The KEEP flow — human-ratified retention of an improving experiment (H1-KEEP).

Mirrors :mod:`test_revert`: an improving experiment no longer AUTO-keeps — it parks
in ``KEEP_PENDING`` and ASKS the human, then on ``t3 outer resolve-keep`` reaches the
terminal ``KEPT`` state, freeing the max-concurrent slot. ``ask_keep`` consults the
taint-floored ``approval_policy`` seam so #119 can auto-answer an owner-taint keep
WITHOUT relaxing the ``record_kept`` consumed-question guard.
"""

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import DeferredQuestion, FactoryScoreSnapshot, OuterLoopExperiment, ProposalSpec, Ticket
from teatree.core.models.approval_policy import Decision
from teatree.loops.outer_loop.keep import ask_keep, resolve_keep


def _keep_pending() -> OuterLoopExperiment:
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
    exp.request_keep(post_snapshot=_snapshot(), merged_sha="feed", reason="target improved 0.20")
    return exp


def _snapshot() -> FactoryScoreSnapshot:
    return FactoryScoreSnapshot.objects.create(
        overlay="", window_days=7, recipe_sha="s", aggregate=0.85, verdict="ok", coverage=1.0, coverage_floor=0.6
    )


class TestKeepFlow(TestCase):
    def test_ask_records_the_keep_question(self) -> None:
        exp = _keep_pending()
        question = ask_keep(exp)
        assert exp.keep_question_id == question.pk
        assert question.is_pending  # the empty #116 dial ASKs — no auto-answer
        assert exp.state == OuterLoopExperiment.State.KEEP_PENDING  # ask does not terminate

    def test_resolve_reaches_terminal_kept_and_frees_the_slot(self) -> None:
        exp = _keep_pending()
        assert OuterLoopExperiment.objects.active_count() == 1  # holds the concurrency slot
        resolve_keep(exp)
        reloaded = OuterLoopExperiment.objects.get(pk=exp.pk)
        assert reloaded.state == OuterLoopExperiment.State.KEPT
        assert reloaded.merged_sha == "feed"
        assert reloaded.decision == OuterLoopExperiment.Decision.KEEP
        assert reloaded.is_terminal
        # Slot freed → a second experiment can now start.
        assert OuterLoopExperiment.objects.active_count() == 0

    def test_resolve_asks_first_when_the_tick_has_not(self) -> None:
        # An operator resolving before the tick asked still lands KEPT (resolve
        # creates + consumes the audit question itself).
        exp = _keep_pending()
        assert exp.keep_question is None
        resolve_keep(exp)
        assert OuterLoopExperiment.objects.get(pk=exp.pk).state == OuterLoopExperiment.State.KEPT

    def test_resolve_skips_reconsume_when_question_already_answered(self) -> None:
        # The tick asked and the human answered the question directly; resolve then
        # only records the keep (no double-consume).
        exp = _keep_pending()
        question = ask_keep(exp)
        DeferredQuestion.consume(question.pk, answer="kept")
        exp.keep_question = DeferredQuestion.objects.get(pk=question.pk)
        resolve_keep(exp)
        assert OuterLoopExperiment.objects.get(pk=exp.pk).state == OuterLoopExperiment.State.KEPT

    def test_auto_approve_dial_answers_the_keep_question(self) -> None:
        # The #119 seam: a permissive owner-taint dial auto-answers the recorded
        # question (resolved_via policy) WITHOUT bypassing the record_kept guard —
        # the guard still sees a consumed answer, only recorded by policy.
        exp = _keep_pending()
        question = ask_keep(exp, dial=lambda _action_class: Decision.AUTO_APPROVE)
        assert DeferredQuestion.objects.get(pk=question.pk).answered_at is not None
        resolve_keep(exp)
        assert OuterLoopExperiment.objects.get(pk=exp.pk).state == OuterLoopExperiment.State.KEPT
