"""Settled owner answers are reused before a new card reaches Slack."""

import dataclasses
from unittest.mock import patch

from django.db import DatabaseError
from django.test import TestCase

from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.models import DeferredQuestion, Session, Task
from teatree.core.notify_question_drains import drain_unmirrored_deferred_questions
from tests._owner_channel import owner_card
from tests.factories import planned_ticket


class TestQuestionCoordinator(TestCase):
    def test_same_architecture_question_on_another_ticket_uses_owner_answer(self) -> None:
        question = "Should we adopt the new dependency?"
        basis = DeferredQuestion.record(
            question,
            card=owner_card(),
            task_session=Session.objects.create(ticket=planned_ticket()),
        )
        assert basis.apply_answer("Use the new dependency.", resolved_via=DeferredQuestion.ResolvedVia.SLACK)
        pending = DeferredQuestion.record(
            question,
            card=owner_card(),
            task_session=Session.objects.create(ticket=planned_ticket()),
        )

        mirrored, total = drain_unmirrored_deferred_questions()

        pending.refresh_from_db()
        assert (mirrored, total) == (0, 0)
        assert pending.answer_text == "Use the new dependency."
        assert pending.resolved_via == DeferredQuestion.ResolvedVia.AGENT
        audit = pending.audits.get(action="coordinated")
        assert str(basis.pk) in audit.note
        assert audit.resolver_id == "coordinator"

    def test_hard_limit_question_stays_pending_despite_matching_owner_answer(self) -> None:
        question = "May I use the production credential?"
        basis = DeferredQuestion.record(question, card=owner_card(OwnerDecision.CREDENTIALS))
        assert basis.apply_answer("Yes.", resolved_via=DeferredQuestion.ResolvedVia.SLACK)
        pending = DeferredQuestion.record(question, card=owner_card(OwnerDecision.CREDENTIALS))

        drain_unmirrored_deferred_questions()

        pending.refresh_from_db()
        assert pending.is_pending
        assert not pending.audits.filter(action="coordinated").exists()

    def test_different_options_stay_pending(self) -> None:
        question = "Should we adopt the new dependency?"
        basis = DeferredQuestion.record(question, card=owner_card())
        assert basis.apply_answer("Yes.", resolved_via=DeferredQuestion.ResolvedVia.SLACK)
        pending = DeferredQuestion.record(question, card=dataclasses.replace(owner_card(), options=()))

        _mirrored, total = drain_unmirrored_deferred_questions()

        pending.refresh_from_db()
        assert total == 1
        assert pending.is_pending

    def test_same_question_with_different_checked_facts_stays_pending(self) -> None:
        question = "Should we adopt the new dependency?"
        basis = DeferredQuestion.record(
            question,
            card=dataclasses.replace(owner_card(), checked=("The package is actively maintained.",)),
        )
        assert basis.apply_answer("Yes.", resolved_via=DeferredQuestion.ResolvedVia.SLACK)
        pending = DeferredQuestion.record(
            question,
            card=dataclasses.replace(owner_card(), checked=("The package is no longer maintained.",)),
        )

        _mirrored, total = drain_unmirrored_deferred_questions()

        assert total == 1
        assert DeferredQuestion.objects.get(pk=pending.pk).is_pending

    def test_same_question_on_different_subject_stays_pending(self) -> None:
        question = "Should we adopt the new dependency?"
        basis = DeferredQuestion.record(question, card=dataclasses.replace(owner_card(), subject="Widget scheduling"))
        assert basis.apply_answer("Yes.", resolved_via=DeferredQuestion.ResolvedVia.SLACK)
        pending = DeferredQuestion.record(
            question, card=dataclasses.replace(owner_card(), subject="Payment processing")
        )

        _mirrored, total = drain_unmirrored_deferred_questions()

        assert total == 1
        assert DeferredQuestion.objects.get(pk=pending.pk).is_pending

    def test_database_read_failure_leaves_card_pending_and_logs(self) -> None:
        pending = DeferredQuestion.record("Should we adopt the new dependency?", card=owner_card())

        with (
            patch("teatree.core.question_coordinator.matching_owner_basis", side_effect=DatabaseError("offline")),
            self.assertLogs("teatree.core.question_coordinator", "WARNING") as captured,
        ):
            _mirrored, total = drain_unmirrored_deferred_questions()

        assert total == 1
        assert DeferredQuestion.objects.get(pk=pending.pk).is_pending
        assert "owner card remains pending" in captured.output[0]

    def test_coordinated_answer_queues_parked_task_resume(self) -> None:
        question = "Should we adopt the new dependency?"
        basis = DeferredQuestion.record(question, card=owner_card())
        assert basis.apply_answer("Yes.", resolved_via=DeferredQuestion.ResolvedVia.SLACK)
        ticket = planned_ticket()
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="coding")
        pending = DeferredQuestion.record(question, card=owner_card(), parked_task=task)

        drain_unmirrored_deferred_questions()

        pending.refresh_from_db()
        assert pending.status == DeferredQuestion.STATUS_ANSWERED
        child = task.child_tasks.get()
        assert child.execution_reason.startswith("The user answered your earlier question: Yes.")
