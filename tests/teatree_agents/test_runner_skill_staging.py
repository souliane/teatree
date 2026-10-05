"""Skill-stage refusals persist a failed attempt before opening a harness."""

from django.test import TestCase

from teatree.agents.runner_skill_staging import stage_skills_or_refusal
from teatree.agents.skill_bundle import ArchitecturalReviewSkillMissingError
from teatree.core.models import Session, Task, TaskAttempt, Ticket


class TestSkillStageRefusal(TestCase):
    def test_missing_skill_records_failure_without_a_harness(self) -> None:
        ticket = Ticket.objects.create()
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket))

        def missing_skill(_phase: str) -> list[str]:
            message = "architectural review skill missing"
            raise ArchitecturalReviewSkillMissingError(message)

        attempt = stage_skills_or_refusal(task, phase="reviewing", stage_skills=missing_skill)

        task.refresh_from_db()
        assert isinstance(attempt, TaskAttempt)
        assert attempt.error == "architectural review skill missing"
        assert task.status == Task.Status.FAILED
