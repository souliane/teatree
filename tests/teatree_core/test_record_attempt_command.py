"""``tasks record-attempt`` — the sub-agent result-envelope hand-off.

Pins that a claimed task's structured result envelope drives it to its terminal
state through the SHARED recorder: the schema-key check, the #1284 phase-evidence
gate, then ``complete`` (auto-advancing the ticket) or ``fail``.
"""

import json
from io import StringIO

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import DeferredQuestion, Session, Task, Ticket


class TestRecordAttemptCommand(TestCase):
    def _claimed_task(self, *, phase: str = "coding") -> Task:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.PLANNED)
        session = Session.objects.create(ticket=ticket, agent_id=phase)
        task = Task.objects.create(ticket=ticket, session=session, phase=phase)
        task.claim(claimed_by="loop-slot")
        return task

    def test_records_success_envelope_and_completes_task(self) -> None:
        task = self._claimed_task()
        result_json = json.dumps({"summary": "done", "files_modified": [{"path": "a.py", "action": "modified"}]})
        out = StringIO()

        call_command("tasks", "record-attempt", str(task.pk), result_json, agent_session_id="sess-1", stdout=out)

        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        attempt = task.attempts.latest("pk")
        assert attempt.result["summary"] == "done"
        assert attempt.agent_session_id == "sess-1"
        assert task.ticket.state == Ticket.State.CODED
        # souliane/teatree#657: an in-session sub-agent always rides the
        # user's Max subscription seat — never the metered lane.
        assert attempt.lane == "subscription"

    def test_outage_death_fails_task_without_advancing_ticket(self) -> None:
        task = self._claimed_task()
        result_json = json.dumps(
            {"summary": "Unable to connect to API", "files_modified": [{"path": "a.py", "action": "modified"}]},
        )

        call_command("tasks", "record-attempt", str(task.pk), result_json, stdout=StringIO())

        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert task.ticket.state == Ticket.State.PLANNED
        assert task.attempts.latest("pk").error.startswith("outage_death:")

    def test_missing_phase_evidence_fails_task(self) -> None:
        task = self._claimed_task()
        result_json = json.dumps({"summary": "no files changed"})

        # A valid-but-evidence-light envelope is recorded as a FAILED attempt
        # (a clean refusal, not a CLI error), so the command exits 0.
        call_command("tasks", "record-attempt", str(task.pk), result_json, stdout=StringIO())

        task.refresh_from_db()
        # coding requires files_modified evidence (#1284) → fail, not complete.
        assert task.status == Task.Status.FAILED

    def test_needs_user_input_completes_and_records_a_deferred_question(self) -> None:
        task = self._claimed_task()
        result_json = json.dumps({"summary": "blocked", "needs_user_input": True, "user_input_reason": "design call"})
        call_command("tasks", "record-attempt", str(task.pk), result_json, stdout=StringIO())

        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        # There is no terminal to ask at, so the STOP parks as a durable question.
        assert DeferredQuestion.objects.filter(parked_task=task).exists()

    def test_invalid_json_rejected(self) -> None:
        task = self._claimed_task()
        with pytest.raises(SystemExit):
            call_command("tasks", "record-attempt", str(task.pk), "not json", stderr=StringIO())
        task.refresh_from_db()
        assert task.status == Task.Status.CLAIMED

    def test_unexpected_keys_fail_task(self) -> None:
        task = self._claimed_task()
        result_json = json.dumps({"summary": "ok", "bogus": 1})
        call_command("tasks", "record-attempt", str(task.pk), result_json, stdout=StringIO())
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED

    def test_unclaimed_task_rejected(self) -> None:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR)
        session = Session.objects.create(ticket=ticket, agent_id="coding")
        task = Task.objects.create(ticket=ticket, session=session, phase="coding")
        # PENDING (not claimed) — recording must be refused.
        with pytest.raises(SystemExit):
            call_command("tasks", "record-attempt", str(task.pk), json.dumps({"summary": "x"}), stderr=StringIO())
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING

    def test_finished_task_rejected(self) -> None:
        task = self._claimed_task()
        task.complete()
        with pytest.raises(SystemExit):
            call_command("tasks", "record-attempt", str(task.pk), json.dumps({"summary": "x"}), stderr=StringIO())
