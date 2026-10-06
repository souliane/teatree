"""A dispatch on a ticket with no worktree yet still carries its repo's stack skills.

Planning is queued beside provisioning, so most planning runs start before the
worktree exists. The detection root then fell back to the drain process's cwd,
which holds no project file, and the bundle carried no framework skill at all.
"""

from pathlib import Path

import pytest
from django.test import TestCase

from teatree.agents.runner import run_agent
from teatree.core.models import Session, Task, Ticket
from tests.teatree_agents._sdk_fake import fake_sdk, success_stream


class TestNoWorktreeDispatchCarriesTheStackSkills(TestCase):
    @pytest.fixture(autouse=True)
    def _empty_cwd_and_django_clone(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # The suite's own cwd is a Django checkout: without the chdir a cwd fallback passes by accident.
        drain_cwd = tmp_path / "drain-cwd"
        drain_cwd.mkdir()
        monkeypatch.chdir(drain_cwd)
        workspace = tmp_path / "clones"
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))
        clone = workspace / "souliane" / "teatree"
        (clone / ".git").mkdir(parents=True)
        (clone / "pyproject.toml").write_text('[project]\ndependencies = ["django>=6"]\n', encoding="utf-8")

    def _task(self, phase: str) -> Task:
        ticket = Ticket.objects.create(repos=["souliane/teatree"])
        return Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase=phase)

    def test_planning_dispatch_without_worktree_records_both_stack_skills(self) -> None:
        task = self._task("planning")
        with fake_sdk(success_stream({"summary": "planned", "plan_text": "the plan"})):
            attempt = run_agent(task, phase="planning", overlay_skill_metadata={})

        assert {"ac-django", "ac-python"} <= set(attempt.skills_loaded)
