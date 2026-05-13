"""Ticket.role: schedule helpers + FSM short-circuit for reviewer-role tickets."""

import pytest
from django.test import TestCase

from teatree.core.models import Task, Ticket
from teatree.core.models.errors import InvalidTransitionError
from teatree.core.models.ticket import schedule_external_review


class TestTicketRoleField(TestCase):
    def test_role_defaults_to_author(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", issue_url="https://example.com/issues/1")
        assert ticket.role == Ticket.Role.AUTHOR

    def test_can_create_reviewer_role_ticket(self) -> None:
        ticket = Ticket.objects.create(
            overlay="acme",
            issue_url="https://example.com/pr/1",
            role=Ticket.Role.REVIEWER,
        )
        assert ticket.role == Ticket.Role.REVIEWER


class TestScheduleExternalReview(TestCase):
    def test_creates_reviewing_task_for_reviewer_ticket(self) -> None:
        ticket = Ticket.objects.create(
            overlay="acme",
            issue_url="https://example.com/pr/2",
            role=Ticket.Role.REVIEWER,
        )

        task = schedule_external_review(ticket)

        assert task.phase == "reviewing"
        assert task.execution_target == Task.ExecutionTarget.HEADLESS
        assert task.ticket_id == ticket.pk

    def test_refuses_author_ticket(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", issue_url="https://example.com/issues/3")
        with pytest.raises(InvalidTransitionError):
            schedule_external_review(ticket)


class TestScheduleCodingRefusesReviewer(TestCase):
    def test_schedule_coding_blocks_reviewer_role(self) -> None:
        ticket = Ticket.objects.create(
            overlay="acme",
            issue_url="https://example.com/pr/4",
            role=Ticket.Role.REVIEWER,
        )
        with pytest.raises(InvalidTransitionError):
            ticket.schedule_coding()


class TestMarkReviewedExternally(TestCase):
    def test_transitions_to_delivered_on_review_complete(self) -> None:
        ticket = Ticket.objects.create(
            overlay="acme",
            issue_url="https://example.com/pr/5",
            role=Ticket.Role.REVIEWER,
            extra={"reviewed_sha": "deadbeef"},
        )
        task = schedule_external_review(ticket)

        task.complete()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.DELIVERED
        # ``mark_reviewed`` upserts the same reviewer ticket via the DB —
        # head sha + last review state should be persisted on ``extra``.
        assert ticket.extra["reviewed_sha"] == "deadbeef"
        assert ticket.extra["last_review_state"] == "approved"

    def test_does_not_advance_author_ticket(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", issue_url="https://example.com/issues/6")
        task = ticket.schedule_coding()

        # Author coding tasks should NOT trigger the reviewer short-circuit.
        task.complete()
        ticket.refresh_from_db()
        # Author flow: state stays NOT_STARTED until ticket.start() is called by the orchestrator.
        assert ticket.state != Ticket.State.DELIVERED
