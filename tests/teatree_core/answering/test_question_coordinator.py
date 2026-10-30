"""Settled owner answers are reused before a new card reaches Slack."""

import dataclasses
from unittest.mock import MagicMock, patch

from django.db import DatabaseError
from django.test import TestCase

from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.models import DeferredQuestion, Session, Task, Ticket
from teatree.core.models.question_text import question_fingerprint
from teatree.core.notify_question_drains import drain_unmirrored_deferred_questions
from teatree.core.question_heal import live_owner_questions
from teatree.utils.url_slug import slack_conversation_anchor
from tests._owner_channel import owner_card
from tests.factories import planned_ticket


class TestQuestionCoordinator(TestCase):
    def test_coordinated_answer_closes_an_already_posted_card(self) -> None:
        question = "Should we adopt the new dependency?"
        basis = DeferredQuestion.record(question, card=owner_card())
        assert basis.apply_answer("Yes.", resolved_via=DeferredQuestion.ResolvedVia.SLACK)
        pending = DeferredQuestion.record(question, card=owner_card())
        assert pending.mark_mirrored(channel="D123", slack_ts="1800000000.000100")
        backend = MagicMock()

        with patch("teatree.core.notify.resolve_owner_dm_backend", return_value=(backend, None)):
            live_owner_questions()

        pending.refresh_from_db()
        assert pending.answer_text == "Yes."
        backend.update_message.assert_called_once()
        assert "You answered: Yes." in backend.update_message.call_args.kwargs["text"]

    def test_headless_producer_markers_share_an_exact_answer_across_tickets(self) -> None:
        question = "Should we adopt the new dependency?"
        first_ticket = planned_ticket()
        second_ticket = planned_ticket()
        fingerprint = question_fingerprint(question)
        basis = DeferredQuestion.record(
            question,
            card=owner_card(),
            task_session=Session.objects.create(ticket=first_ticket),
            dedupe_marker=f"needs-input:{first_ticket.pk}:{fingerprint}",
        )
        assert basis.apply_answer("Yes.", resolved_via=DeferredQuestion.ResolvedVia.SLACK)
        pending = DeferredQuestion.record(
            question,
            card=owner_card(),
            task_session=Session.objects.create(ticket=second_ticket),
            dedupe_marker=f"needs-input:{second_ticket.pk}:{fingerprint}",
        )

        _mirrored, total = drain_unmirrored_deferred_questions()

        assert total == 0
        assert DeferredQuestion.objects.get(pk=pending.pk).answer_text == "Yes."

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
        for kind, question in (
            (OwnerDecision.CREDENTIALS, "May I use the production credential?"),
            (OwnerDecision.MONEY_OR_PLAN, "May I buy the additional service plan?"),
            (OwnerDecision.PUBLIC_POST, "May I post this statement to a colleague?"),
            (OwnerDecision.IRREVERSIBLE, "May I delete the only copy of this work?"),
        ):
            with self.subTest(kind=kind):
                basis = DeferredQuestion.record(question, card=owner_card(kind))
                assert basis.apply_answer("Yes.", resolved_via=DeferredQuestion.ResolvedVia.SLACK)
                pending = DeferredQuestion.record(question, card=owner_card(kind))

                drain_unmirrored_deferred_questions()

                pending.refresh_from_db()
                assert pending.is_pending
                assert not pending.audits.filter(action="coordinated").exists()

    def test_agent_answer_is_not_an_owner_basis(self) -> None:
        question = "Should we adopt the new dependency?"
        basis = DeferredQuestion.record(question, card=owner_card())
        assert basis.apply_answer("Yes.", resolved_via=DeferredQuestion.ResolvedVia.AGENT)
        pending = DeferredQuestion.record(question, card=owner_card())

        _mirrored, total = drain_unmirrored_deferred_questions()

        assert total == 1
        assert DeferredQuestion.objects.get(pk=pending.pk).is_pending
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

    def test_same_question_with_different_proposed_text_stays_pending(self) -> None:
        question = "Should we adopt the new dependency?"
        basis = DeferredQuestion.record(
            question, card=dataclasses.replace(owner_card(), quoted="Use the first library for queues.")
        )
        assert basis.apply_answer("Yes.", resolved_via=DeferredQuestion.ResolvedVia.SLACK)
        pending = DeferredQuestion.record(
            question, card=dataclasses.replace(owner_card(), quoted="Use the second library for queues.")
        )

        _mirrored, total = drain_unmirrored_deferred_questions()

        assert total == 1
        assert DeferredQuestion.objects.get(pk=pending.pk).is_pending
        assert not pending.audits.filter(action="coordinated").exists()

    def test_same_question_with_the_same_proposed_text_still_uses_the_owner_answer(self) -> None:
        question = "Should we adopt the new dependency?"
        card = dataclasses.replace(owner_card(), quoted="Use the first library for queues.")
        basis = DeferredQuestion.record(question, card=card)
        assert basis.apply_answer("Yes.", resolved_via=DeferredQuestion.ResolvedVia.SLACK)
        pending = DeferredQuestion.record(question, card=card)

        _mirrored, total = drain_unmirrored_deferred_questions()

        assert total == 0
        assert pending.audits.filter(action="coordinated").exists()

    def test_rules_file_card_always_reaches_the_owner(self) -> None:
        question = "Should the rules file let agents merge on green checks?"
        card = dataclasses.replace(owner_card(), quoted="Agents merge when every check is green.")
        basis = DeferredQuestion.record(question, card=card)
        assert basis.apply_answer("Yes.", resolved_via=DeferredQuestion.ResolvedVia.SLACK)
        pending = DeferredQuestion.record(question, card=card)

        _mirrored, total = drain_unmirrored_deferred_questions()

        assert total == 1
        assert DeferredQuestion.objects.get(pk=pending.pk).is_pending
        assert not pending.audits.filter(action="coordinated").exists()

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

    def test_action_card_is_not_answered_from_another_owner_action(self) -> None:
        question = "May I waive this merge item?"
        basis = DeferredQuestion.record(question, card=owner_card())
        basis.evidence = {
            **basis.evidence,
            "action": {"kind": "merge_item_waiver", "target": "first", "binding": "one"},
        }
        basis.save(update_fields=["evidence"])
        assert basis.apply_answer("Yes", resolved_via=DeferredQuestion.ResolvedVia.SLACK)
        pending = DeferredQuestion.record(question, card=owner_card())
        pending.evidence = {
            **pending.evidence,
            "action": {"kind": "merge_item_waiver", "target": "second", "binding": "two"},
        }
        pending.save(update_fields=["evidence"])

        _mirrored, total = drain_unmirrored_deferred_questions()

        assert total == 1
        assert DeferredQuestion.objects.get(pk=pending.pk).is_pending

    def test_distinct_producer_markers_do_not_share_an_owner_answer(self) -> None:
        question = "May I lift the override?"
        basis = DeferredQuestion.record(question, card=owner_card(), dedupe_marker="override-lift:first")
        assert basis.apply_answer("Yes", resolved_via=DeferredQuestion.ResolvedVia.SLACK)
        pending = DeferredQuestion.record(question, card=owner_card(), dedupe_marker="override-lift:second")

        _mirrored, total = drain_unmirrored_deferred_questions()

        assert total == 1
        assert DeferredQuestion.objects.get(pk=pending.pk).is_pending

    def test_record_does_not_reuse_an_answer_from_a_different_producer_on_the_same_subject(self) -> None:
        question = "May I lift the override?"
        card = dataclasses.replace(owner_card(), subject="https://github.com/o/r/issues/10")
        basis = DeferredQuestion.record(question, card=card, dedupe_marker="override-lift:first")
        assert basis.apply_answer("Yes", resolved_via=DeferredQuestion.ResolvedVia.SLACK)

        pending = DeferredQuestion.record(question, card=card, dedupe_marker="override-lift:second")
        assert pending.pk != basis.pk
        assert pending.is_pending

        _mirrored, total = drain_unmirrored_deferred_questions()
        assert total == 1
        assert DeferredQuestion.objects.get(pk=pending.pk).is_pending

    def test_database_read_failure_leaves_card_pending_and_logs(self) -> None:
        pending = DeferredQuestion.record("Should we adopt the new dependency?", card=owner_card())

        with (
            patch(
                "teatree.core.answering.question_coordinator.matching_owner_basis",
                side_effect=DatabaseError("offline"),
            ),
            self.assertLogs("teatree.core.answering.question_coordinator", "WARNING") as captured,
        ):
            _mirrored, total = drain_unmirrored_deferred_questions()

        assert total == 1
        assert DeferredQuestion.objects.get(pk=pending.pk).is_pending
        assert "owner card remains pending" in captured.output[0]

    def test_answer_committed_before_a_lost_result_is_not_mirrored(self) -> None:
        question = "Should we adopt the new dependency?"
        basis = DeferredQuestion.record(question, card=owner_card())
        assert basis.apply_answer("Yes", resolved_via=DeferredQuestion.ResolvedVia.SLACK)
        pending = DeferredQuestion.record(question, card=owner_card())
        original_apply = DeferredQuestion.apply_answer

        def commit_without_result(row: DeferredQuestion, answer: str, *, resolved_via: str) -> None:
            original_apply(row, answer, resolved_via=resolved_via)

        with patch.object(DeferredQuestion, "apply_answer", commit_without_result):
            _mirrored, total = drain_unmirrored_deferred_questions()

        assert total == 0
        assert not DeferredQuestion.objects.get(pk=pending.pk).is_pending

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

    def test_a_conversation_build_task_keeps_the_coordinated_answer_and_resumes_nothing(self) -> None:
        question = "Should we adopt the new dependency?"
        basis = DeferredQuestion.record(question, card=owner_card())
        assert basis.apply_answer("Yes.", resolved_via=DeferredQuestion.ResolvedVia.SLACK)
        ticket = Ticket.objects.create(issue_url=slack_conversation_anchor(channel="D1", slack_ts="1.0"))
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="coding")
        pending = DeferredQuestion.record(question, card=owner_card(), parked_task=task)

        with self.assertLogs("teatree.core.answering.question_coordinator", "WARNING"):
            drain_unmirrored_deferred_questions()

        pending.refresh_from_db()
        assert pending.status == DeferredQuestion.STATUS_ANSWERED
        assert pending.audits.filter(action="coordinated").exists()
        assert not task.child_tasks.exists()

    def test_failed_resume_keeps_the_owner_question_pending(self) -> None:
        question = "Should we adopt the new dependency?"
        basis = DeferredQuestion.record(question, card=owner_card())
        assert basis.apply_answer("Yes.", resolved_via=DeferredQuestion.ResolvedVia.SLACK)
        ticket = planned_ticket()
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="coding")
        pending = DeferredQuestion.record(question, card=owner_card(), parked_task=task)

        with (
            patch(
                "teatree.core.answering.question_coordinator.schedule_resume",
                side_effect=DatabaseError("resume failed"),
            ),
            self.assertLogs("teatree.core.answering.question_coordinator", "WARNING"),
        ):
            _mirrored, total = drain_unmirrored_deferred_questions()

        pending.refresh_from_db()
        assert total == 1
        assert pending.is_pending
        assert not pending.audits.filter(action="coordinated").exists()
        assert not task.child_tasks.exists()
