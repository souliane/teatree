"""A dispatch on a ticket with no worktree yet still carries its repo's stack skills.

Planning is queued beside provisioning, so most planning runs start before the
worktree exists. The detection root then fell back to the drain process's cwd,
which holds no project file, and the bundle carried no framework skill at all.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.agents import runner_preparation, skill_assurance
from teatree.agents.runner import run_agent
from teatree.agents.skill_assurance import assess_skill_dispatch
from teatree.core.models import Session, Task
from teatree.skill_support import index as skill_index
from teatree.skill_support.index import DEFAULT_SKILLS_DIR
from tests.factories import planned_ticket
from tests.teatree_agents._sdk_fake import fake_sdk, success_stream

_STACK = frozenset({"ac-django", "ac-python"})

_CODE_PHASES = (
    "planning",
    "coding",
    "testing",
    "reviewing",
    "critic_reviewing",
    "codex_reviewing",
    "debugging",
    "shipping",
    "architectural_review",
    "answering",
)


class TestEveryCodePhaseRequiresBothStackSkills(TestCase):
    """A stack skill shown only as a "Read <path> when it applies" line was read 0 of 36 times."""

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
        (clone / "manage.py").write_text("# django project\n", encoding="utf-8")
        review_skill = Path.home() / ".claude" / "skills" / "code-review"
        review_skill.mkdir(parents=True)
        (review_skill / "SKILL.md").write_text("# code-review\n", encoding="utf-8")

    def _task(self, phase: str) -> Task:
        ticket = planned_ticket(repos=["souliane/teatree"], overlay="t3-teatree")
        return Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase=phase)

    def test_each_phase_records_both_and_requires_loading_both(self) -> None:
        for phase in _CODE_PHASES:
            with self.subTest(phase=phase):
                with (
                    fake_sdk(success_stream({"summary": "done"})),
                    patch.object(runner_preparation, "assess_skill_dispatch", wraps=assess_skill_dispatch) as assess,
                ):
                    attempt = run_agent(self._task(phase), phase=phase, overlay_skill_metadata={})
                rendered = assess.call_args.kwargs["rendered_context"]

                assert set(attempt.skills_loaded) >= _STACK
                assert set(attempt.result["skill_assurance"]["explicit_load"]) >= _STACK
                assert not [line for line in rendered.splitlines() if "/ac-" in line and "when it applies" in line]

    def test_a_missing_stack_skill_refuses_the_dispatch_before_the_harness_opens(self) -> None:
        only_django = Path(self.enterContext(TemporaryDirectory()))
        (only_django / "ac-django").mkdir()
        (only_django / "ac-django" / "SKILL.md").write_text("# ac-django\n", encoding="utf-8")
        with (
            fake_sdk(success_stream({"summary": "should not run"})) as fake,
            patch.object(skill_assurance, "harness_skills_dirs", return_value=[DEFAULT_SKILLS_DIR, only_django]),
            patch.object(skill_index, "install_roots", return_value=[only_django, *skill_index.install_roots()]),
        ):
            attempt = run_agent(self._task("planning"), phase="planning", overlay_skill_metadata={})
            opened = fake.last_options

        assert opened is None
        assert attempt.result["skill_assurance"]["status"] == "missing"
        assert attempt.result["skill_assurance"]["missing"] == ["ac-python"]
