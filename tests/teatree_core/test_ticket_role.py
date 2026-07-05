"""Ticket.role: schedule helpers + FSM short-circuit for reviewer-role tickets."""

import pytest
from django.test import TestCase

from teatree.core.models import Task, Ticket
from teatree.core.models.errors import InvalidTransitionError
from teatree.core.models.ticket_external_review import schedule_external_review


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
        # (reviewer, reviewing) → t3:reviewer is loop-dispatched → in-session.
        assert task.execution_target == Task.ExecutionTarget.INTERACTIVE
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


class TestMarkReviewNoAction(TestCase):
    """#1077: terminal disposition for a no-postable-action external review."""

    def test_consumes_pending_task_and_records_no_action_state(self) -> None:
        """The PENDING reviewing task is terminated and the real outcome stamped.

        Anti-vacuity anchor: pre-#1077 there is no ``mark_review_no_action``
        transition, so this test cannot even resolve the attribute — RED by
        ``AttributeError``. With the fix: ticket → DELIVERED, the reviewing
        Task is COMPLETED (no longer PENDING — the infinite re-queue stops),
        and ``last_review_state`` is ``reviewed_no_action`` (NEVER
        ``approved``, so a later genuine review is not suppressed).
        """
        ticket = Ticket.objects.create(
            overlay="acme",
            issue_url="https://gitlab/x/-/merge_requests/1077",
            role=Ticket.Role.REVIEWER,
            extra={"reviewed_sha": "sha1"},
        )
        task = schedule_external_review(ticket)
        assert task.status == Task.Status.PENDING

        ticket.mark_review_no_action()
        ticket.save()

        ticket.refresh_from_db()
        task.refresh_from_db()
        assert ticket.state == Ticket.State.DELIVERED
        assert task.status == Task.Status.COMPLETED
        assert ticket.extra["last_review_state"] == "reviewed_no_action"
        assert ticket.extra["last_review_state"] != "approved"
        assert ticket.extra["reviewed_sha"] == "sha1"

    def test_refuses_author_role_ticket(self) -> None:
        from django_fsm import TransitionNotAllowed  # noqa: PLC0415

        ticket = Ticket.objects.create(
            overlay="acme",
            issue_url="https://example.com/issues/7",
            extra={"reviewed_sha": "sha1"},
        )
        with pytest.raises(TransitionNotAllowed):
            ticket.mark_review_no_action()

    def test_no_extra_write_when_url_or_sha_missing(self) -> None:
        """Body's ``if self.issue_url and sha`` guard — still terminal, no stamp."""
        ticket = Ticket.objects.create(
            overlay="acme",
            issue_url="https://gitlab/x/-/merge_requests/1078",
            role=Ticket.Role.REVIEWER,
        )
        task = schedule_external_review(ticket)

        ticket.mark_review_no_action()
        ticket.save()

        ticket.refresh_from_db()
        task.refresh_from_db()
        assert ticket.state == Ticket.State.DELIVERED
        assert task.status == Task.Status.COMPLETED
        assert "last_review_state" not in (ticket.extra or {})
