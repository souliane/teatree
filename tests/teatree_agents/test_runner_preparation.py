"""The prepared work brief names the skills whose application the receipt checks."""

from types import SimpleNamespace

from django.test import TestCase

from teatree.agents._runner_env import DispatchCredential
from teatree.agents.harness_dispatch import DispatchHarness
from teatree.agents.harness_registry import HarnessCapabilities
from teatree.agents.prompt import build_task_prompt
from teatree.agents.runner import run_agent
from teatree.agents.runner_preparation import Preflight, prepare_run
from teatree.core.models import Session, Task
from tests.factories import planned_ticket
from tests.teatree_agents._sdk_fake import FakeHarnessSession, fake_sdk, success_stream


class TestPreparedSkillBrief(TestCase):
    def test_the_brief_names_every_requested_skill(self) -> None:
        ticket = planned_ticket(repos=["souliane/teatree"], overlay="t3-teatree")
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="coding")

        with fake_sdk(success_stream({"summary": "done"})):
            attempt = run_agent(task, phase="coding", overlay_skill_metadata={})

        requested = attempt.result["skill_assurance"]["requested"]
        lines = [line for line in FakeHarnessSession.last_prompt.splitlines() if "skill_application" in line]
        assert requested
        assert len(lines) == 1
        assert all(name in lines[0] for name in requested)

    def test_the_brief_is_unchanged_when_no_skill_is_requested(self) -> None:
        ticket = planned_ticket()
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="retro")
        dispatch = DispatchHarness(
            harness=SimpleNamespace(capabilities=HarnessCapabilities(managed_lane=True)),
            name="codex_app_server",
            provider=None,
            model=None,
        )

        prepared = prepare_run(
            task,
            Preflight(stage_skills=[], dispatch=dispatch, skills=[]),
            phase="retro",
            handoff=None,
            credential=DispatchCredential(),
        )

        assert prepared.skill_assurance["requested"] == []
        assert prepared.prompt == build_task_prompt(task, skills=[], stage_skills=[])
