from django.test import TestCase

from teatree.core.models import DeferredQuestion, PlanArtifact, Session, Task, Ticket
from teatree.core.models.self_improve_wedge_repair import repair_legacy_self_improve_wedges


class LegacyWedgeRepairTests(TestCase):
    def test_old_tool_use_marker_is_repaired_and_dismissed(self) -> None:
        ticket = Ticket.objects.create(extra={"source": "self_improve"})
        session = Session.objects.create(ticket=ticket, agent_id="planning")
        Task.objects.create(ticket=ticket, session=session, phase="planning", status=Task.Status.COMPLETED)
        PlanArtifact.objects.create(
            ticket=ticket,
            plan_text="repair plan",
            recorded_by="test",
            base_sha="a" * 40,
            adequacy={
                section: {"none_reason": "legacy plan was already accepted"}
                for section in ("design", "integration_seams", "edge_cases", "test_strategy", "acceptance_criteria")
            },
        )
        question = DeferredQuestion.record(
            "legacy planning wedge", task_session=session, tool_use_id=f"fsm-wedge:{ticket.pk}:planning"
        )

        result = repair_legacy_self_improve_wedges(apply=True)

        ticket.refresh_from_db()
        question.refresh_from_db()
        assert result["repaired"] == [ticket.pk]
        assert ticket.state == Ticket.State.PLAN_RECORDED
        assert question.dismissed_at is not None
