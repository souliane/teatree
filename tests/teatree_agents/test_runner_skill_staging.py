"""Skill-stage refusals persist a failed attempt before opening a harness."""

from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from teatree.agents.runner_skill_staging import stage_skills_or_refusal, staged_skills_or_refusal
from teatree.agents.skill_bundle import ArchitecturalReviewSkillMissingError
from teatree.core.models import Session, Task, TaskAttempt, Ticket
from teatree.skill_support import index as skill_index


def _task(phase: str = "reviewing") -> Task:
    ticket = Ticket.objects.create()
    return Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase=phase)


class TestSkillStageRefusal(TestCase):
    def test_missing_skill_records_failure_without_a_harness(self) -> None:
        task = _task()

        def missing_skill(_phase: str) -> list[str]:
            message = "architectural review skill missing"
            raise ArchitecturalReviewSkillMissingError(message)

        attempt = stage_skills_or_refusal(task, phase="reviewing", stage_skills=missing_skill, bundle=list)

        task.refresh_from_db()
        assert isinstance(attempt, TaskAttempt)
        assert attempt.error == "architectural review skill missing"
        assert task.status == Task.Status.FAILED

    def test_a_local_folder_shadowing_a_declared_pin_refuses_the_dispatch(self) -> None:
        task = _task()
        local = Path.home() / "repo-skills"
        (local / "ac-django").mkdir(parents=True)
        (local / "ac-django" / "SKILL.md").write_text("---\nname: ac-django\n---\n", encoding="utf-8")
        (local / "rules").mkdir()
        (local / "rules" / "SKILL.md").write_text("---\nname: rules\n---\n", encoding="utf-8")

        with patch.object(skill_index, "DEFAULT_SKILLS_DIR", local):
            attempt = staged_skills_or_refusal(task, phase="reviewing", overlay_skill_metadata={})

        task.refresh_from_db()
        assert isinstance(attempt, TaskAttempt)
        assert "souliane/skills/ac-django#" in attempt.error
        assert str(local / "ac-django" / "SKILL.md") in attempt.error
        assert task.status == Task.Status.FAILED

    def test_without_a_shadow_the_bundle_is_staged(self) -> None:
        task = _task()
        local = Path.home() / "repo-skills"
        (local / "rules").mkdir(parents=True)
        (local / "rules" / "SKILL.md").write_text("---\nname: rules\n---\n", encoding="utf-8")

        with patch.object(skill_index, "DEFAULT_SKILLS_DIR", local):
            staged = staged_skills_or_refusal(task, phase="reviewing", overlay_skill_metadata={})

        assert not isinstance(staged, TaskAttempt)
        assert "rules" in staged[1]
