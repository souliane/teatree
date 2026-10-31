"""An escalation is one internal request row; its deadline raises an owner card or fails the asker (#5685)."""

import os
from datetime import timedelta

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.answering.escalation import (
    ESCALATION_DEADLINE,
    INTERNAL_KIND,
    MARKER_PREFIX,
    UNSERVED_PREFIX,
    EscalationRefusedError,
    coordinator_task,
    open_request,
    record_request,
    resume_coordinator,
    settle_expired_escalations,
    settle_request,
)
from teatree.core.modelkit.owner_decision import OWNER_RAISABLE_DECISIONS, OwnerDecision
from teatree.core.models import Task, Ticket
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.loop.question_drain import drain_pending_questions
from tests.factories import SessionFactory, TaskFactory, TicketFactory

_MESSAGE = "Which of the two migration orders should I follow"


def _asker(**task: object) -> Task:
    ticket = TicketFactory(state=Ticket.State.WORK_STARTED)
    session = SessionFactory(ticket=ticket, agent_id="planning")
    return TaskFactory(ticket=ticket, session=session, phase="planning", status=Task.Status.CLAIMED, **task)


def _age(row: DeferredQuestion, by: timedelta) -> None:
    DeferredQuestion.objects.filter(pk=row.pk).update(created_at=timezone.now() - by)


class TestTheRequestRow(TestCase):
    def test_a_call_writes_one_internal_row_parked_on_the_asking_task(self) -> None:
        asker = _asker()

        row = record_request(asker, kind=INTERNAL_KIND, message=_MESSAGE)

        assert DeferredQuestion.objects.count() == 1
        assert row.audience == DeferredQuestion.Audience.INTERNAL
        assert row.parked_task_id == asker.pk
        assert row.question == _MESSAGE
        assert row.dedupe_marker.startswith(MARKER_PREFIX)
        assert row.evidence == {"escalation": {"kind": INTERNAL_KIND, "task": asker.pk}}
        assert row.is_pending

    def test_the_same_call_again_returns_the_same_row(self) -> None:
        asker = _asker()

        first = record_request(asker, kind=INTERNAL_KIND, message=_MESSAGE)
        again = record_request(asker, kind=INTERNAL_KIND, message=_MESSAGE)

        assert again.pk == first.pk
        assert DeferredQuestion.objects.count() == 1

    def test_another_message_or_another_task_is_another_request(self) -> None:
        asker = _asker()
        other = _asker()

        rows = {
            record_request(asker, kind=INTERNAL_KIND, message=_MESSAGE).pk,
            record_request(asker, kind=INTERNAL_KIND, message=f"{_MESSAGE} first").pk,
            record_request(other, kind=INTERNAL_KIND, message=_MESSAGE).pk,
            record_request(asker, kind=OwnerDecision.ARCHITECTURE, message=_MESSAGE).pk,
        }

        assert len(rows) == 4

    def test_an_unknown_kind_is_refused_naming_the_valid_ones_and_writes_nothing(self) -> None:
        with pytest.raises(EscalationRefusedError) as raised:
            record_request(_asker(), kind="spend_money", message=_MESSAGE)

        assert all(kind in str(raised.value) for kind in (INTERNAL_KIND, *OwnerDecision))
        assert not DeferredQuestion.objects.exists()

    def test_a_blank_message_is_refused(self) -> None:
        with pytest.raises(EscalationRefusedError):
            record_request(_asker(), kind=INTERNAL_KIND, message="   ")


class TestOpenRequests(TestCase):
    def test_a_pending_request_is_open_for_its_tickets_thread_only(self) -> None:
        asker = _asker()
        row = record_request(asker, kind=INTERNAL_KIND, message=_MESSAGE)

        assert open_request(asker.ticket_id) == row
        assert open_request(_asker().ticket_id) is None

    def test_an_answered_or_dismissed_request_is_no_longer_open(self) -> None:
        for resolve in (
            lambda row: row.apply_answer("go with the first", resolved_via="agent"),
            lambda row: row.mark_stale("asker moved on"),
        ):
            asker = _asker()
            row = record_request(asker, kind=INTERNAL_KIND, message=_MESSAGE)
            resolve(row)

            assert open_request(asker.ticket_id) is None

    def test_a_request_past_its_deadline_is_no_longer_open(self) -> None:
        asker = _asker()
        row = record_request(asker, kind=INTERNAL_KIND, message=_MESSAGE)

        _age(row, ESCALATION_DEADLINE + timedelta(seconds=1))

        assert open_request(asker.ticket_id) is None

    def test_the_newest_open_request_of_a_ticket_is_the_one_returned(self) -> None:
        asker = _asker()
        record_request(asker, kind=INTERNAL_KIND, message=_MESSAGE)
        newest = record_request(asker, kind=INTERNAL_KIND, message=f"{_MESSAGE} again")

        assert open_request(asker.ticket_id) == newest

    def test_an_owner_audience_row_is_never_a_request_whatever_its_marker(self) -> None:
        asker = _asker()
        DeferredQuestion.objects.create(
            question="Is this fine?",
            audience=DeferredQuestion.Audience.OWNER_QUESTION,
            dedupe_marker=f"{MARKER_PREFIX}forged",
            parked_task=asker,
        )

        assert open_request(asker.ticket_id) is None

    def test_an_owner_card_on_the_same_ticket_is_not_a_request(self) -> None:
        asker = _asker()
        DeferredQuestion.record("Is this fine?", parked_task=asker, dedupe_marker="needs-input:1:abc")

        assert open_request(asker.ticket_id) is None


class TestTheThreadsCoordinatorTask(TestCase):
    def test_it_is_the_newest_answering_task_on_the_ticket(self) -> None:
        ticket = TicketFactory(state=Ticket.State.WORK_STARTED)
        session = SessionFactory(ticket=ticket, agent_id="answering")
        TaskFactory(ticket=ticket, session=session, phase="answering", status=Task.Status.COMPLETED)
        newest = TaskFactory(ticket=ticket, session=session, phase="answering", status=Task.Status.COMPLETED)
        TaskFactory(ticket=ticket, session=session, phase="planning")

        assert coordinator_task(ticket.pk) == newest

    def test_a_ticket_with_no_answering_task_has_none(self) -> None:
        assert coordinator_task(_asker().ticket_id) is None

    def test_resuming_it_queues_one_child_carrying_the_request(self) -> None:
        asker = _asker()
        parent = TaskFactory(
            ticket=asker.ticket,
            session=SessionFactory(ticket=asker.ticket, agent_id="answering"),
            phase="answering",
            status=Task.Status.COMPLETED,
        )

        child = resume_coordinator(asker.ticket_id, event_id="escalation:7", message="the fenced request")
        again = resume_coordinator(asker.ticket_id, event_id="escalation:7", message="the fenced request")

        assert child is not None
        assert again == child
        assert child.parent_task_id == parent.pk
        assert child.phase == "answering"
        assert "the fenced request" in child.execution_reason

    def test_a_second_request_joins_the_continuation_still_queued(self) -> None:
        asker = _asker()
        TaskFactory(
            ticket=asker.ticket,
            session=SessionFactory(ticket=asker.ticket, agent_id="answering"),
            phase="answering",
            status=Task.Status.COMPLETED,
        )

        first = resume_coordinator(asker.ticket_id, event_id="escalation:7", message="first request")
        second = resume_coordinator(asker.ticket_id, event_id="owner-reply:9", message="owner reply")

        assert first is not None
        assert second == first
        assert "first request" in second.execution_reason
        assert "owner reply" in second.execution_reason
        assert Task.objects.filter(phase="answering").count() == 2

    def test_a_repeat_after_the_continuation_ran_queues_nothing_new(self) -> None:
        asker = _asker()
        TaskFactory(
            ticket=asker.ticket,
            session=SessionFactory(ticket=asker.ticket, agent_id="answering"),
            phase="answering",
            status=Task.Status.COMPLETED,
        )
        child = resume_coordinator(asker.ticket_id, event_id="escalation:7", message="the request")
        assert child is not None
        Task.objects.filter(pk=child.pk).update(status=Task.Status.COMPLETED)

        assert resume_coordinator(asker.ticket_id, event_id="escalation:7", message="the request") == child
        assert Task.objects.filter(phase="answering").count() == 2

    def test_nothing_is_resumed_when_the_ticket_has_no_answering_task(self) -> None:
        asker = _asker()

        assert resume_coordinator(asker.ticket_id, event_id="escalation:7", message="x") is None
        assert Task.objects.filter(parent_task__isnull=False).count() == 0


class TestTheDeadline(TestCase):
    def _expired(self, kind: str, asker: Task | None = None) -> tuple[Task, DeferredQuestion]:
        asker = asker or _asker()
        row = record_request(asker, kind=kind, message=_MESSAGE)
        _age(row, ESCALATION_DEADLINE + timedelta(minutes=1))
        row.refresh_from_db()
        return asker, row

    def test_an_owner_raisable_kind_raises_exactly_one_owner_card_and_closes_the_request(self) -> None:
        asker, row = self._expired(OwnerDecision.ARCHITECTURE)

        assert settle_expired_escalations() == 1
        assert settle_expired_escalations() == 0

        cards = DeferredQuestion.owner_pending()
        assert cards.count() == 1
        card = cards.get()
        assert card.parked_task_id == asker.pk
        assert card.evidence["decision"] == "architecture"
        assert _MESSAGE in card.evidence["quoted"]
        row.refresh_from_db()
        assert not row.is_pending
        assert "owner" in row.dismissed_reason
        asker.refresh_from_db()
        assert asker.status == Task.Status.CLAIMED

    def test_every_raisable_kind_makes_a_card_the_owner_checks_accept(self) -> None:
        for decision in OWNER_RAISABLE_DECISIONS:
            with self.subTest(decision=decision):
                DeferredQuestion.objects.all().delete()
                self._expired(decision)

                settle_expired_escalations()

                assert DeferredQuestion.owner_pending().get().evidence["decision"] == decision.value

    def test_a_kind_that_is_not_owner_raisable_fails_the_asker_with_a_typed_reason_and_no_card(self) -> None:
        for kind in (INTERNAL_KIND, OwnerDecision.PRODUCT_SCOPE.value):
            asker, row = self._expired(kind)

            assert settle_expired_escalations() == 1

            asker.refresh_from_db()
            assert asker.status == Task.Status.FAILED
            assert asker.failure_reason.startswith(UNSERVED_PREFIX)
            row.refresh_from_db()
            assert not row.is_pending
        assert not DeferredQuestion.owner_pending().exists()

    def test_the_raised_card_is_keyed_by_its_request(self) -> None:
        _asker_task, row = self._expired(OwnerDecision.ARCHITECTURE)

        settle_expired_escalations()

        assert DeferredQuestion.owner_pending().get().dedupe_marker == f"esc-owner:{row.pk}"

    def test_failing_the_asker_leaves_a_live_holders_claim_for_its_own_unwind(self) -> None:
        asker = _asker(claimed_by="worker-1", owner_pid=os.getpid())
        _asker_task, _row = self._expired(INTERNAL_KIND, asker)

        settle_expired_escalations()

        asker.refresh_from_db()
        assert asker.status == Task.Status.FAILED
        assert asker.claimed_by == "worker-1"

    def test_a_request_whose_asking_task_is_gone_is_closed_without_a_card(self) -> None:
        asker, row = self._expired(OwnerDecision.ARCHITECTURE)
        DeferredQuestion.objects.filter(pk=row.pk).update(parked_task=None)

        assert settle_expired_escalations() == 1

        assert not DeferredQuestion.objects.get(pk=row.pk).is_pending
        assert not DeferredQuestion.owner_pending().exists()
        asker.refresh_from_db()
        assert asker.status == Task.Status.CLAIMED

    def test_a_request_that_stopped_pending_before_it_was_settled_is_not_settled_again(self) -> None:
        _asker_task, row = self._expired(OwnerDecision.ARCHITECTURE)
        row.apply_answer("go with the first", resolved_via="agent")

        assert settle_request(row) is False
        assert not DeferredQuestion.owner_pending().exists()

    def test_a_request_inside_its_deadline_is_left_alone(self) -> None:
        asker = _asker()
        record_request(asker, kind=OwnerDecision.ARCHITECTURE, message=_MESSAGE)

        assert settle_expired_escalations() == 0
        assert not DeferredQuestion.owner_pending().exists()
        asker.refresh_from_db()
        assert asker.status == Task.Status.CLAIMED

    def test_a_request_whose_asker_already_finished_is_closed_without_a_card_or_a_failure(self) -> None:
        asker, row = self._expired(OwnerDecision.ARCHITECTURE)
        Task.objects.filter(pk=asker.pk).update(status=Task.Status.COMPLETED)

        assert settle_expired_escalations() == 1

        row.refresh_from_db()
        assert not row.is_pending
        assert not DeferredQuestion.owner_pending().exists()
        asker.refresh_from_db()
        assert asker.status == Task.Status.COMPLETED

    def test_an_answered_request_is_never_settled(self) -> None:
        _asker_task, row = self._expired(OwnerDecision.ARCHITECTURE)
        row.apply_answer("go with the first", resolved_via="agent")

        assert settle_expired_escalations() == 0
        assert not DeferredQuestion.owner_pending().exists()


class TestTheTickDrainLeavesAnOpenRequestAlone(TestCase):
    def test_a_request_inside_its_deadline_survives_the_drain(self) -> None:
        asker = _asker()
        row = record_request(asker, kind=INTERNAL_KIND, message=_MESSAGE)

        drain_pending_questions()

        row.refresh_from_db()
        assert row.is_pending
        assert open_request(asker.ticket_id) == row
