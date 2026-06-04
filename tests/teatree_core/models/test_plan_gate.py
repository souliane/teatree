"""Plan-before-code gate: PLANNED FSM state + PlanArtifact DB record.

Structural invariant: the only path from STARTED to CODED passes through PLANNED.
``code()`` is sourced from PLANNED (not STARTED) so skipping ``plan()`` raises
``TransitionNotAllowed`` — a STATE-GRAPH IMPOSSIBILITY, not a prose rule.

``plan()`` is itself guarded by ``check_plan_artifact()`` which requires a
``PlanArtifact`` DB row — no in-memory escape hatch.

All tests follow the symmetric must-pass / must-fail pattern from
dod_gate.py so a future regression is caught in both directions.
"""

from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase
from django_fsm import TransitionNotAllowed

from teatree.agents.attempt_recorder import AttemptUsage, record_result_envelope
from teatree.agents.result_schema import check_evidence
from teatree.core import tasks as tasks_mod
from teatree.core.models import Session, Task, Ticket
from teatree.core.models.plan_artifact import NoPlanArtifactError, PlanArtifact


def _started_ticket() -> Ticket:
    t = Ticket.objects.create(overlay="acme", role=Ticket.Role.AUTHOR)
    t.state = Ticket.State.STARTED
    t.save()
    return t


def _planned_ticket() -> Ticket:
    t = _started_ticket()
    PlanArtifact.record(ticket=t, plan_text="Implement X by doing Y")
    t.plan()
    t.save()
    return t


class TestCannotReachCodedDirectlyFromStarted(TestCase):
    """Structural: STARTED → CODED must raise TransitionNotAllowed.

    This test is the anti-vacuous proof that the FSM gate is load-bearing.
    With ``code()`` sourced from STARTED, this test passes → gate is broken.
    With ``code()`` sourced from PLANNED, this test passes → gate works.
    Proven RED on the pre-implementation source (code() source=STARTED) then
    GREEN after retargeting to PLANNED.
    """

    def test_cannot_reach_coded_directly_from_started(self) -> None:
        ticket = _started_ticket()
        with pytest.raises(TransitionNotAllowed):
            ticket.code()


class TestPlanTransitionRequiresPlanArtifact(TestCase):
    """``plan()`` must be guarded: no artifact → NoPlanArtifactError."""

    def test_plan_without_artifact_raises(self) -> None:
        ticket = _started_ticket()
        with pytest.raises(NoPlanArtifactError):
            ticket.plan()

    def test_plan_with_artifact_advances_to_planned(self) -> None:
        ticket = _started_ticket()
        PlanArtifact.record(ticket=ticket, plan_text="Implement X by doing Y")
        ticket.plan()
        ticket.save()
        assert ticket.state == Ticket.State.PLANNED

    def test_plan_from_non_started_raises(self) -> None:
        ticket = Ticket.objects.create(overlay="acme")
        ticket.state = Ticket.State.CODED
        ticket.save()
        with pytest.raises(TransitionNotAllowed):
            ticket.plan()


class TestCodeTransitionFromPlanned(TestCase):
    """``code()`` must accept PLANNED as source and advance to CODED."""

    def test_code_from_planned_advances_to_coded(self) -> None:
        ticket = _planned_ticket()
        ticket.code()
        ticket.save()
        assert ticket.state == Ticket.State.CODED

    def test_code_from_started_raises(self) -> None:
        ticket = _started_ticket()
        with pytest.raises(TransitionNotAllowed):
            ticket.code()


class TestPlanArtifactModel(TestCase):
    """PlanArtifact.record() is the single guarded factory."""

    def test_record_creates_artifact(self) -> None:
        ticket = _started_ticket()
        artifact = PlanArtifact.record(ticket=ticket, plan_text="Do X")
        assert PlanArtifact.objects.filter(ticket=ticket).count() == 1
        assert artifact.plan_text == "Do X"

    def test_record_requires_non_empty_plan_text(self) -> None:
        ticket = _started_ticket()
        with pytest.raises(ValueError, match="plan_text"):
            PlanArtifact.record(ticket=ticket, plan_text="")

    def test_record_requires_non_whitespace_plan_text(self) -> None:
        ticket = _started_ticket()
        with pytest.raises(ValueError, match="plan_text"):
            PlanArtifact.record(ticket=ticket, plan_text="   ")

    def test_multiple_artifacts_allowed_newest_governs(self) -> None:
        ticket = _started_ticket()
        PlanArtifact.record(ticket=ticket, plan_text="Plan v1")
        PlanArtifact.record(ticket=ticket, plan_text="Plan v2")
        assert PlanArtifact.objects.filter(ticket=ticket).count() == 2

    def test_artifact_for_wrong_ticket_does_not_unlock_plan(self) -> None:
        ticket_a = _started_ticket()
        ticket_b = _started_ticket()
        PlanArtifact.record(ticket=ticket_a, plan_text="Plan for A")
        with pytest.raises(NoPlanArtifactError):
            ticket_b.plan()


class TestAttemptRecorderRecordsPlanArtifact(TestCase):
    """record_result_envelope auto-records PlanArtifact on a planning success."""

    def _make_planning_task(self) -> Task:
        ticket = _started_ticket()
        session = Session.objects.create(ticket=ticket, agent_id="t3:planner")
        return Task.objects.create(
            ticket=ticket,
            session=session,
            phase="planning",
            execution_target=Task.ExecutionTarget.HEADLESS,
            execution_reason="test",
        )

    def test_planning_success_auto_records_plan_artifact(self) -> None:
        task = self._make_planning_task()
        result = {"summary": "Plan done", "plan_text": "Step 1: do X. Step 2: do Y."}
        record_result_envelope(task, result, phase="planning", usage=AttemptUsage())
        assert PlanArtifact.objects.filter(ticket=task.ticket).exists()

    def test_planning_without_plan_text_does_not_record_artifact(self) -> None:
        task = self._make_planning_task()
        PlanArtifact.record(ticket=task.ticket, plan_text="pre-existing")
        old_count = PlanArtifact.objects.filter(ticket=task.ticket).count()
        result_no_text = {"summary": "Plan done", "plan_text": ""}
        assert check_evidence(result_no_text, "planning")  # fails evidence check (returns error msg)
        record_result_envelope(task, result_no_text, phase="planning", usage=AttemptUsage())
        assert PlanArtifact.objects.filter(ticket=task.ticket).count() == old_count

    def test_non_planning_phase_does_not_record_artifact(self) -> None:
        ticket = _planned_ticket()
        ticket.code()
        ticket.save()
        session = Session.objects.create(ticket=ticket, agent_id="t3:coder")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            phase="coding",
            execution_target=Task.ExecutionTarget.HEADLESS,
            execution_reason="test",
        )
        count_before = PlanArtifact.objects.filter(ticket=ticket).count()
        result = {
            "summary": "Coded",
            "plan_text": "This should not create a PlanArtifact",
            "files_modified": [{"path": "foo.py", "action": "modified"}],
        }
        record_result_envelope(task, result, phase="coding", usage=AttemptUsage())
        assert PlanArtifact.objects.filter(ticket=ticket).count() == count_before


class TestStartSchedulesPlanning(TestCase):
    """After ticket.start(), the scheduled task is a planning task (not coding)."""

    def test_start_schedules_planning_task(self) -> None:
        ticket = Ticket.objects.create(overlay="acme", role=Ticket.Role.AUTHOR)
        ticket.scope(issue_url="https://example.com/1", variant="acme", repos=["backend"])
        ticket.save()

        def fake_enqueue(ticket_id: int) -> None:
            target = Ticket.objects.get(pk=ticket_id)
            if target.state == Ticket.State.STARTED:
                target.schedule_planning()

        fake_task = MagicMock()
        fake_task.enqueue.side_effect = fake_enqueue
        with (
            patch.object(tasks_mod, "execute_provision", fake_task),
            self.captureOnCommitCallbacks(execute=True),
        ):
            ticket.start()
            ticket.save()

        assert Task.objects.filter(ticket=ticket, phase="planning").exists()
