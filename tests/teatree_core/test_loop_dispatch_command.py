"""Tests for the ``loop_dispatch`` management command (pending-spawn / spawn-claim)."""

import json
from io import StringIO

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import Session, Task, Ticket
from teatree.core.models.ticket import schedule_external_review


class _LoopDispatchTest(TestCase):
    def _reviewer_task(self, *, url: str = "https://example.com/pr/1", head_sha: str = "x") -> Task:
        ticket = Ticket.objects.create(
            overlay="acme",
            issue_url=url,
            role=Ticket.Role.REVIEWER,
            extra={"reviewed_sha": head_sha},
        )
        return schedule_external_review(ticket)

    def _author_task(self, *, url: str = "https://example.com/issues/9") -> Task:
        ticket = Ticket.objects.create(overlay="acme", issue_url=url, role=Ticket.Role.AUTHOR)
        return ticket.schedule_coding()


class TestPendingSpawn(_LoopDispatchTest):
    def test_emits_reviewer_subagent_for_reviewer_role(self) -> None:
        task = self._reviewer_task()
        stdout = StringIO()
        call_command("loop_dispatch", "pending-spawn", "--json", stdout=stdout)

        payload = json.loads(stdout.getvalue())
        assert len(payload) == 1
        entry = payload[0]
        assert entry["task_id"] == task.pk
        assert entry["subagent"] == "t3:reviewer"
        assert entry["phase"] == "reviewing"
        assert entry["ticket_role"] == Ticket.Role.REVIEWER
        assert entry["issue_url"] == "https://example.com/pr/1"

    def test_emits_orchestrator_subagent_for_author_coding(self) -> None:
        task = self._author_task()
        stdout = StringIO()
        call_command("loop_dispatch", "pending-spawn", "--json", stdout=stdout)

        payload = json.loads(stdout.getvalue())
        assert len(payload) == 1
        assert payload[0]["task_id"] == task.pk
        assert payload[0]["subagent"] == "t3:orchestrator"

    def test_skips_claimed_tasks(self) -> None:
        task = self._reviewer_task()
        task.claim(claimed_by="loop-slot")
        stdout = StringIO()
        call_command("loop_dispatch", "pending-spawn", "--json", stdout=stdout)

        assert json.loads(stdout.getvalue()) == []

    def test_skips_tasks_with_no_registered_subagent(self) -> None:
        # A scoping task on an author ticket → no _SUBAGENT_BY_PHASE entry → skipped.
        ticket = Ticket.objects.create(overlay="acme", issue_url="https://example.com/issues/77")
        session = Session.objects.create(ticket=ticket, agent_id="scoping")
        Task.objects.create(ticket=ticket, session=session, phase="scoping")
        stdout = StringIO()
        call_command("loop_dispatch", "pending-spawn", "--json", stdout=stdout)

        assert json.loads(stdout.getvalue()) == []

    def test_text_output_when_empty(self) -> None:
        stdout = StringIO()
        call_command("loop_dispatch", "pending-spawn", stdout=stdout)
        assert "No pending spawn requests." in stdout.getvalue()


class TestSpawnClaim(_LoopDispatchTest):
    def test_claims_pending_task(self) -> None:
        task = self._reviewer_task()
        stdout = StringIO()
        call_command("loop_dispatch", "spawn-claim", str(task.pk), stdout=stdout)

        task.refresh_from_db()
        assert task.status == Task.Status.CLAIMED
        assert task.claimed_by == "loop-slot"
        assert "Claimed task" in stdout.getvalue()

    def test_unknown_task_errors(self) -> None:
        with pytest.raises(SystemExit):
            call_command("loop_dispatch", "spawn-claim", "999999")

    def test_claim_with_custom_worker(self) -> None:
        task = self._reviewer_task()
        call_command("loop_dispatch", "spawn-claim", str(task.pk), claimed_by="custom-worker")
        task.refresh_from_db()
        assert task.claimed_by == "custom-worker"
