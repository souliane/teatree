"""A recent dispatch on a Python/Django repo that ran without the stack skills it calls for shows on the chip."""

from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.factory import stack_skill_coverage
from teatree.core.factory.health_signal import HealthSignal
from teatree.core.factory.operational_health import collect_signals
from teatree.core.models import Session, Task, TaskAttempt, Ticket


class TestStackSkillCoverageSignal(TestCase):
    @pytest.fixture(autouse=True)
    def _clones(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        workspace = tmp_path / "clones"
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))
        django_clone = workspace / "souliane" / "teatree"
        (django_clone / ".git").mkdir(parents=True)
        (django_clone / "manage.py").write_text("# django project\n", encoding="utf-8")
        (workspace / "acme" / "docs-site" / ".git").mkdir(parents=True)

    def _attempt(self, *, repo: str, skills_loaded: list[str]) -> Task:
        ticket = Ticket.objects.create(repos=[repo])
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="planning")
        TaskAttempt.objects.create(task=task, skills_loaded=skills_loaded)
        return task

    def _signal(self) -> HealthSignal | None:
        return next((s for s in collect_signals().signals if s.fingerprint == "stack-skills-missing"), None)

    def test_a_django_dispatch_with_no_stack_skill_names_both(self) -> None:
        task = self._attempt(repo="souliane/teatree", skills_loaded=["internals", "architecture-design"])

        signal = self._signal()

        assert signal is not None
        assert signal.kind == "stack_skills_missing"
        assert "ac-django, ac-python" in signal.summary
        assert str(task.pk) in signal.summary

    def test_an_ac_django_only_bundle_names_ac_python(self) -> None:
        self._attempt(repo="souliane/teatree", skills_loaded=["ac-django", "architecture-design"])

        signal = self._signal()

        assert signal is not None
        assert "ac-python" in signal.summary
        assert "ac-django," not in signal.summary

    def test_a_bundle_carrying_both_raises_nothing(self) -> None:
        self._attempt(repo="souliane/teatree", skills_loaded=["ac-django", "ac-python", "architecture-design"])

        assert self._signal() is None

    def test_a_repo_with_no_python_project_raises_nothing(self) -> None:
        self._attempt(repo="acme/docs-site", skills_loaded=["architecture-design"])

        assert self._signal() is None

    def test_an_attempt_that_recorded_no_bundle_is_not_judged(self) -> None:
        self._attempt(repo="souliane/teatree", skills_loaded=[])

        assert self._signal() is None

    def test_a_read_that_raises_names_the_collector_unread(self) -> None:
        self._attempt(repo="souliane/teatree", skills_loaded=["internals"])

        with patch.object(stack_skill_coverage, "dispatch_detection_root", side_effect=OSError("disk gone")):
            collected = collect_signals()

        assert "stack_skill_coverage_signals" in collected.unread
        assert not [s for s in collected.signals if s.fingerprint == "stack-skills-missing"]
