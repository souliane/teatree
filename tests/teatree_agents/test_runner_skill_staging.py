"""Skill-stage refusals persist a failed attempt before opening a harness."""

from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.agents.runner_skill_staging import stage_skills_or_refusal, staged_skills_or_refusal
from teatree.agents.skill_bundle import ArchitecturalReviewSkillMissingError
from teatree.core.models import Session, Task, TaskAttempt, Ticket
from teatree.skill_support import index as skill_index
from tests._unreadable_apm_manifest import unreadable_running_manifest


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


class TestUnreadablePinsPark(TestCase):
    @staticmethod
    def _stage(task: Task) -> tuple[list[str], list[str]] | TaskAttempt:
        local = Path.home() / "repo-skills"
        (local / "rules").mkdir(parents=True, exist_ok=True)
        (local / "rules" / "SKILL.md").write_text("---\nname: rules\n---\n", encoding="utf-8")
        with unreadable_running_manifest(Path.home()), patch.object(skill_index, "DEFAULT_SKILLS_DIR", local):
            return staged_skills_or_refusal(task, phase="reviewing", overlay_skill_metadata={})

    def test_an_unreadable_manifest_parks_the_task_under_the_limit_park_marker(self) -> None:
        task = _task()
        earliest = timezone.now() + timedelta(seconds=300)

        attempt = self._stage(task)

        task.refresh_from_db()
        assert isinstance(attempt, TaskAttempt)
        assert attempt.error.startswith("limit_parked: pins_unreadable: ")
        assert str(Path.home() / "apm.yml") in attempt.error
        assert task.status == Task.Status.PENDING
        assert task.not_before is not None
        assert earliest <= task.not_before <= timezone.now() + timedelta(seconds=300)

    def test_a_repeated_park_folds_into_one_attempt_row(self) -> None:
        task = _task()

        self._stage(task)
        self._stage(task)

        assert task.attempts.get().park_repeats == 1
