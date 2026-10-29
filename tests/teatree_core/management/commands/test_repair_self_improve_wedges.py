from typing import cast
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django_fsm import TransitionNotAllowed

from teatree.core.models import DeferredQuestion, PlanArtifact, Session, Task, Ticket
from teatree.core.models.errors import InvalidTransitionError, NoCurrentPlanError


class RepairSelfImproveWedgesTests(TestCase):
    def _wedge(
        self, *, source: str = "self_improve", phase: str = "planning", plan: bool = True
    ) -> tuple[Ticket, DeferredQuestion]:
        ticket = Ticket.objects.create(extra={"source": source})
        session = Session.objects.create(ticket=ticket, agent_id="planning")
        task = Task.objects.create(ticket=ticket, session=session, phase="planning", status=Task.Status.COMPLETED)
        if plan:
            PlanArtifact.objects.create(
                ticket=ticket,
                plan_text="repair plan",
                recorded_by="test",
                base_sha="a" * 40,
                adequacy={
                    "design": {"content": "repair the ticket FSM"},
                    "integration_seams": {"content": ["src/teatree/core/models/ticket.py"]},
                    "edge_cases": {"content": "legacy wedge"},
                    "test_strategy": {"content": "check the repaired state"},
                    "acceptance_criteria": {"none_reason": "the legacy plan declared no criteria"},
                },
            )
        question = DeferredQuestion.record(
            "planning wedge",
            task_session=session,
            dedupe_marker=f"fsm-wedge:{ticket.pk}:{phase}:{task.pk}",
        )
        return ticket, question

    def test_apply_repairs_only_selected_wedges_and_dry_run_writes_nothing(self) -> None:
        target, question = self._wedge()
        unrelated, other_question = self._wedge(source="manual")
        wrong_phase, wrong_question = self._wedge(phase="coding")
        missing_plan, missing_question = self._wedge(plan=False)
        thin_plan, thin_question = self._wedge(plan=False)
        PlanArtifact.objects.create(ticket=thin_plan, plan_text="legacy thin plan", recorded_by="test")

        preview = cast("dict[str, list[int]]", call_command("repair_self_improve_wedges"))
        target.refresh_from_db()
        assert preview["repairable"] == [target.pk]
        assert target.state == Ticket.State.NOT_STARTED

        result = cast("dict[str, list[int]]", call_command("repair_self_improve_wedges", "--apply"))
        target.refresh_from_db()
        question.refresh_from_db()
        unrelated.refresh_from_db()
        wrong_phase.refresh_from_db()
        missing_plan.refresh_from_db()
        assert result["repaired"] == [target.pk]
        assert result["missing_plan"] == [missing_plan.pk]
        assert result["invalid_plan"] == [thin_plan.pk]
        assert target.state == Ticket.State.PLAN_RECORDED
        assert question.dismissed_at is not None
        assert unrelated.state == wrong_phase.state == missing_plan.state == Ticket.State.NOT_STARTED
        untouched = (other_question, wrong_question, missing_question, thin_question)
        assert all(row.dismissed_at is None for row in untouched)
        repeated = cast("dict[str, list[int]]", call_command("repair_self_improve_wedges", "--apply"))
        assert repeated["repaired"] == []

    def test_transition_refusal_rolls_back_and_a_rerun_can_repair(self) -> None:
        for error in (NoCurrentPlanError("stale"), InvalidTransitionError("invalid"), TransitionNotAllowed("blocked")):
            with self.subTest(error=type(error).__name__):
                ticket, question = self._wedge()
                with patch.object(Ticket, "plan", side_effect=error):
                    result = cast("dict[str, list[int]]", call_command("repair_self_improve_wedges", "--apply"))
                ticket.refresh_from_db()
                question.refresh_from_db()
                assert ticket.pk in result["invalid_plan"]
                assert ticket.pk not in result["repaired"]
                assert ticket.state == Ticket.State.NOT_STARTED
                assert question.dismissed_at is None
                rerun = cast("dict[str, list[int]]", call_command("repair_self_improve_wedges", "--apply"))
                assert ticket.pk in rerun["repaired"]
