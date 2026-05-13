"""Ticket.role: schedule helpers + FSM short-circuit for reviewer-role tickets."""

import json
import tempfile
from pathlib import Path

import pytest
from django.test import TestCase

from teatree.core.models import Task, Ticket
from teatree.core.models.errors import InvalidTransitionError
from teatree.core.models.ticket import schedule_external_review
from teatree.loop.scanners import reviewer_prs


class _ReviewerCacheTmpMixin:
    """Redirect the reviewer cache to a per-test tmp dir."""

    def setUp(self) -> None:
        super().setUp()
        self._cache_tmp = tempfile.TemporaryDirectory()
        self._cache_path = Path(self._cache_tmp.name) / "reviewer_prs.json"
        self._original_default_path = reviewer_prs._default_cache_path
        reviewer_prs._default_cache_path = lambda: self._cache_path  # ty: ignore[invalid-assignment]

    def tearDown(self) -> None:
        reviewer_prs._default_cache_path = self._original_default_path
        self._cache_tmp.cleanup()
        super().tearDown()


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


class TestMarkReviewedExternally(_ReviewerCacheTmpMixin, TestCase):
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
        cache = json.loads(self._cache_path.read_text())
        assert cache == {"https://example.com/pr/5": "deadbeef"}

    def test_does_not_advance_author_ticket(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", issue_url="https://example.com/issues/6")
        task = ticket.schedule_coding()

        # Author coding tasks should NOT trigger the reviewer short-circuit.
        task.complete()
        ticket.refresh_from_db()
        # Author flow: state stays NOT_STARTED until ticket.start() is called by the orchestrator.
        assert ticket.state != Ticket.State.DELIVERED
