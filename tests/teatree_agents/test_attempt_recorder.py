"""Shared result-envelope recorder used by both dispatch backends."""

import pytest
from django.test import TestCase

from teatree.agents.attempt_recorder import (
    AttemptUsage,
    ResultEnvelopeError,
    parse_result_envelope,
    record_result_envelope,
    validate_result_keys,
)
from teatree.core.models import Session, Task, Ticket


class TestParseResultEnvelope(TestCase):
    def test_parses_object(self) -> None:
        assert parse_result_envelope('{"summary": "ok"}') == {"summary": "ok"}

    def test_rejects_non_object(self) -> None:
        with pytest.raises(ResultEnvelopeError):
            parse_result_envelope("[1, 2]")

    def test_rejects_invalid_json(self) -> None:
        with pytest.raises(ResultEnvelopeError):
            parse_result_envelope("not json")


class TestValidateResultKeys(TestCase):
    def test_accepts_schema_keys(self) -> None:
        assert validate_result_keys({"summary": "x", "tests_passed": 3}) == ""

    def test_rejects_unknown_keys(self) -> None:
        assert "unexpected keys" in validate_result_keys({"bogus": 1})


class TestRecordResultEnvelope(TestCase):
    def _claimed(self, *, phase: str = "coding") -> Task:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id=phase)
        task = Task.objects.create(ticket=ticket, session=session, phase=phase)
        task.claim(claimed_by="loop-slot")
        return task

    def test_success_completes_and_stamps_usage(self) -> None:
        task = self._claimed()
        attempt = record_result_envelope(
            task,
            {"summary": "done", "files_modified": [{"path": "a.py", "action": "modified"}]},
            usage=AttemptUsage(agent_session_id="sess", cost_usd=0.4, num_turns=3),
        )
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        assert attempt.cost_usd == pytest.approx(0.4)
        assert attempt.num_turns == 3
        assert attempt.agent_session_id == "sess"

    def test_evidence_gate_fails_task(self) -> None:
        task = self._claimed()
        record_result_envelope(task, {"summary": "nothing changed"})
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED

    def test_unexpected_keys_fail_task(self) -> None:
        task = self._claimed()
        record_result_envelope(task, {"summary": "x", "bogus": True})
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
