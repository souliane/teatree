"""A wrongly-dismissed question can be put back in the queue (#4748).

Every drain is a single-use CAS, so before this a dismissal was terminal in both
directions: no code path cleared ``dismissed_at``, both CLI verbs guarded on it being
null, and the model was not in the admin. A resolver that dropped a LIVE question
therefore silenced it permanently — and the stuck-redispatch guards count dismissed
rows, so its ticket could never escalate again either.
"""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from teatree.core.models.deferred_question import DeferredQuestion, DeferredQuestionAudit


def _dismissed(reason: str = "the halted lane has since run to success") -> DeferredQuestion:
    question = DeferredQuestion.record("How should this halt proceed?")
    question.mark_stale(reason, resolver_id="halt_trigger_cleared")
    return question


class TestReopen(TestCase):
    def test_a_dismissed_row_returns_to_the_pending_queue(self) -> None:
        question = _dismissed()

        assert question.reopen(note="the coding lane is still halted") is True

        question.refresh_from_db()
        assert question.is_pending
        assert question.dismissed_reason == ""
        assert question.resolved_via == DeferredQuestion.ResolvedVia.UNRESOLVED
        assert question.pk in {row.pk for row in DeferredQuestion.pending()}

    def test_the_reopen_is_audited_with_its_resolver(self) -> None:
        question = _dismissed()

        question.reopen(note="dropped by an over-broad resolver", resolver_id="operator")

        audit = DeferredQuestionAudit.objects.get(question=question, action="reopened")
        assert audit.note == "dropped by an over-broad resolver"
        assert audit.resolver_id == "operator"
        assert audit.dismissed_reason == ""

    def test_the_escalation_ladder_is_reset_so_the_row_is_asked_again(self) -> None:
        question = _dismissed()
        DeferredQuestion.objects.filter(pk=question.pk).update(
            escalation_count=3, escalated_at=timezone.now() - timedelta(days=9)
        )
        question.refresh_from_db()

        question.reopen(note="still halted")

        question.refresh_from_db()
        assert question.escalation_count == 0
        assert question.escalated_at is None

    def test_the_delivered_slack_thread_is_kept_so_a_reply_still_binds(self) -> None:
        question = DeferredQuestion.record("How should this halt proceed?")
        question.mark_mirrored(channel="D123", slack_ts="1700000000.000100")
        question.mark_stale("drained")

        question.reopen(note="still halted")

        question.refresh_from_db()
        assert question.slack_ts == "1700000000.000100"
        assert question.slack_channel == "D123"

    def test_an_answered_row_is_never_reopened(self) -> None:
        # Its answer may already have been applied; resuming the parked task would replay it.
        question = DeferredQuestion.record("How should this halt proceed?")
        DeferredQuestion.consume(question.pk, answer="investigate")

        assert question.reopen(note="changed my mind") is False

        question.refresh_from_db()
        assert question.status == DeferredQuestion.STATUS_ANSWERED

    def test_a_still_pending_row_is_not_a_transition(self) -> None:
        question = DeferredQuestion.record("How should this halt proceed?")

        assert question.reopen(note="already pending") is False
        assert not DeferredQuestionAudit.objects.filter(question=question, action="reopened").exists()

    def test_reopening_twice_transitions_once(self) -> None:
        question = _dismissed()

        assert question.reopen(note="first") is True
        assert question.reopen(note="second") is False
        assert DeferredQuestionAudit.objects.filter(question=question, action="reopened").count() == 1

    def test_a_reopened_row_can_then_be_answered(self) -> None:
        question = _dismissed()
        question.reopen(note="still halted")

        answered = DeferredQuestion.consume(question.pk, answer="rework it")

        assert answered is not None
        assert answered.answer_text == "rework it"
