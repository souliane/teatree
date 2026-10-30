"""The prepared work brief names the skills whose application the receipt checks."""

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from teatree.agents._runner_env import DispatchCredential
from teatree.agents.harness_dispatch import DispatchHarness
from teatree.agents.harness_registry import HarnessCapabilities
from teatree.agents.prompt import build_task_prompt
from teatree.agents.read_only_venue_brief import ReadOnlyFacts
from teatree.agents.runner import _preflight, run_agent
from teatree.agents.runner_preparation import Preflight, prepare_run
from teatree.core.models import Session, Task, TaskAttempt
from teatree.skill_support.pin_shadow import SkillPinRefusalError
from tests.factories import planned_ticket
from tests.teatree_agents._sdk_fake import FakeHarnessSession, fake_sdk, success_stream


class TestPreparedSkillBrief(TestCase):
    def test_executor_sweep_refuses_an_unresolvable_configured_skill_pin(self) -> None:
        ticket = planned_ticket(repos=["souliane/teatree"], overlay="t3-teatree")
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="backlog_sweep")
        task.claim(claimed_by="loop-slot")
        dispatch = DispatchHarness(
            harness=SimpleNamespace(capabilities=HarnessCapabilities(read_only_venue=True)),
            name="codex_app_server",
            provider=None,
            model=None,
        )
        with (
            patch("teatree.agents.runner.prepare_own_pr_review_head", return_value=None),
            patch("teatree.agents.runner.pre_harness_refusal", return_value=None),
            patch("teatree.agents.runner.staged_skills_or_refusal", return_value=([], [])),
            patch("teatree.agents.runner._resolve_backend_or_failure", return_value=dispatch),
            patch(
                "teatree.agents.runner_skill_staging.resolve_executor_sweep_bundle",
                side_effect=SkillPinRefusalError("bad pin"),
            ),
        ):
            refusal = _preflight(task, phase="backlog_sweep", overlay_skill_metadata={})

        assert isinstance(refusal, TaskAttempt)
        assert "bad pin" in refusal.error

    def test_sweep_skill_override_is_applied_only_after_executor_selection(self) -> None:
        ticket = planned_ticket(repos=["souliane/teatree"], overlay="t3-teatree")
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="backlog_sweep")
        settings = SimpleNamespace(backlog_sweep_skill="custom-sweep")

        for read_only in (False, True):
            dispatch = DispatchHarness(
                harness=SimpleNamespace(capabilities=HarnessCapabilities(read_only_venue=read_only)),
                name="codex_app_server" if read_only else "claude_sdk",
                provider=None,
                model=None,
            )
            with (
                patch("teatree.agents.runner.prepare_own_pr_review_head", return_value=None),
                patch("teatree.agents.runner.pre_harness_refusal", return_value=None),
                patch("teatree.agents.runner.staged_skills_or_refusal", return_value=([], [])),
                patch("teatree.agents.runner._resolve_backend_or_failure", return_value=dispatch) as select,
                patch("teatree.agents.skill_bundle.get_effective_settings", return_value=settings),
            ):
                preflight = _preflight(task, phase="backlog_sweep", overlay_skill_metadata={})

            assert isinstance(preflight, Preflight)
            select.assert_called_once_with(task, phase="backlog_sweep", skills=[])
            assert ("custom-sweep" in preflight.skills) is read_only

    def test_claude_sweep_still_embeds_all_declared_skills(self) -> None:
        ticket = planned_ticket(repos=["souliane/teatree"], overlay="t3-teatree")
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="backlog_sweep")
        dispatch = DispatchHarness(
            harness=SimpleNamespace(capabilities=HarnessCapabilities(managed_lane=True)),
            name="claude_sdk",
            provider=None,
            model=None,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("custom-sweep", "companion"):
                skill = root / name
                skill.mkdir()
                (skill / "SKILL.md").write_text(f"# {name}\nFollow this skill.\n")
            with (
                patch("teatree.agents.skill_injection.harness_skills_dirs", return_value=[root]),
                patch("teatree.agents.skill_assurance.harness_skills_dirs", return_value=[root]),
                patch("teatree.agents.runner_preparation._native_compaction_env", return_value=None),
            ):
                prepared = prepare_run(
                    task,
                    Preflight(stage_skills=[], dispatch=dispatch, skills=["custom-sweep", "companion"]),
                    phase="backlog_sweep",
                    handoff=None,
                    credential=DispatchCredential(),
                )

        assert prepared.skill_assurance["injected"] == ["custom-sweep", "companion"]

    def test_custom_sweep_skill_is_delivered_on_the_read_only_executor(self) -> None:
        ticket = planned_ticket(repos=["souliane/teatree"], overlay="t3-teatree")
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="backlog_sweep")
        facts = ReadOnlyFacts("", "", "", "", "", "", "", "")
        dispatch = DispatchHarness(
            harness=SimpleNamespace(capabilities=HarnessCapabilities(read_only_venue=True, managed_lane=True)),
            name="codex_app_server",
            provider=None,
            model=None,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "custom-sweep"
            skill.mkdir()
            (skill / "SKILL.md").write_text(
                "# Custom sweep\n\n## Workflow\nRun commands.\n\n## Decisions\nReturn proposals.\n"
            )
            with (
                patch(
                    "teatree.agents.skill_bundle.get_effective_settings",
                    return_value=SimpleNamespace(backlog_sweep_skill="custom-sweep"),
                ),
                patch("teatree.agents.runner_preparation.ReadOnlyFacts.gather", return_value=facts),
                patch("teatree.agents.skill_injection.harness_skills_dirs", return_value=[root]),
                patch("teatree.agents.skill_assurance.harness_skills_dirs", return_value=[root]),
            ):
                prepared = prepare_run(
                    task,
                    Preflight(stage_skills=[], dispatch=dispatch, skills=["custom-sweep"]),
                    phase="backlog_sweep",
                    handoff=None,
                    credential=DispatchCredential(),
                )

        assert prepared.skill_assurance["injected"] == ["custom-sweep"]
        assert "Run commands." not in prepared.options.system_prompt

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
