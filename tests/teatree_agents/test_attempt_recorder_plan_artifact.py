"""A short-verb ``plan`` task records its PlanArtifact and unwedges the ticket.

Regression for teatree integration audit #20: ``_maybe_record_plan_artifact``
compared the raw phase to ``"planning"``, so a task stored with the accepted
short verb ``"plan"`` never recorded a ``PlanArtifact``. The planning task then
completed, the plan gate refused the ``STARTED → PLANNED`` transition
(``NoPlanArtifactError``), the completion rolled back, and the ticket wedged at
``STARTED`` with coding edits denied. Routing the comparison through
``normalize_phase`` records the artifact so the ticket advances.
"""

import pytest
from django.test import TestCase

from teatree.agents.attempt_recorder import record_result_envelope
from teatree.core.models import Session, Task, Ticket
from teatree.core.models.errors import NoPlanArtifactError
from teatree.core.models.plan_artifact import PlanArtifact


class TestPlanArtifactPhaseAlias(TestCase):
    def _planning_task(self, *, phase: str) -> Task:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id=phase)
        task = Task.objects.create(ticket=ticket, session=session, phase=phase)
        task.claim(claimed_by="loop-slot")
        return task

    def test_short_verb_plan_records_artifact_and_advances(self) -> None:
        task = self._planning_task(phase="plan")
        record_result_envelope(task, {"plan_text": "1. build X\n2. test X"}, phase="plan")
        task.refresh_from_db()
        task.ticket.refresh_from_db()
        assert PlanArtifact.objects.filter(ticket=task.ticket).exists()
        assert task.ticket.state == Ticket.State.PLANNED
        assert task.status == Task.Status.COMPLETED

    def test_canonical_planning_records_artifact_and_advances(self) -> None:
        task = self._planning_task(phase="planning")
        record_result_envelope(task, {"plan_text": "1. build X"}, phase="planning")
        task.refresh_from_db()
        task.ticket.refresh_from_db()
        assert PlanArtifact.objects.filter(ticket=task.ticket).exists()
        assert task.ticket.state == Ticket.State.PLANNED

    def test_ticket_is_not_stranded_at_started(self) -> None:
        # The strand contract: the ticket must leave STARTED, not wedge there.
        task = self._planning_task(phase="plan")
        record_result_envelope(task, {"plan_text": "the plan"}, phase="plan")
        task.ticket.refresh_from_db()
        assert task.ticket.state != Ticket.State.STARTED

    def test_phase_from_task_field_when_envelope_phase_blank(self) -> None:
        # The recorder falls back to ``task.phase`` when no explicit phase is passed.
        task = self._planning_task(phase="plan")
        record_result_envelope(task, {"plan_text": "the plan"})
        assert PlanArtifact.objects.filter(ticket=task.ticket).exists()

    def test_non_planning_phase_records_no_artifact(self) -> None:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="code")
        task = Task.objects.create(ticket=ticket, session=session, phase="code")
        task.claim(claimed_by="loop-slot")
        record_result_envelope(
            task,
            {"plan_text": "ignored", "files_modified": [{"path": "a.py", "action": "modified"}]},
            phase="code",
        )
        assert not PlanArtifact.objects.filter(ticket=ticket).exists()

    def test_empty_plan_text_records_no_artifact(self) -> None:
        task = self._planning_task(phase="plan")
        # A whitespace-only plan is not evidence, so no artifact is recorded and
        # the plan gate then refuses the STARTED → PLANNED advance.
        with pytest.raises(NoPlanArtifactError):
            record_result_envelope(task, {"plan_text": "   "}, phase="plan")
        assert not PlanArtifact.objects.filter(ticket=task.ticket).exists()
