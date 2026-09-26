"""A t3-teatree dispatch, with the overlay's REAL skill metadata, delivers only real skills."""

import tempfile
from pathlib import Path
from types import SimpleNamespace

from django.test import TestCase

from teatree.agents._runner_env import DispatchCredential
from teatree.agents.harness_dispatch import DispatchHarness
from teatree.agents.harness_registry import HarnessCapabilities
from teatree.agents.prompt import build_system_context
from teatree.agents.runner_preparation import Preflight, PreparedRun, prepare_run
from teatree.agents.skill_bundle import resolve_skill_bundle
from teatree.contrib.t3_teatree.overlay import TeatreeOverlay
from teatree.core.models import Session, Task
from teatree.skill_support.loading import SkillLoadingPolicy
from tests.factories import planned_ticket

_TEATREE_CHECKOUT = Path(__file__).resolve().parents[2]
_DISPATCHED_PHASES = ("coding", "codex_reviewing", "codex_adversarial_reviewing")


class TestT3TeatreeSkillDispatch(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = planned_ticket()

    def _task(self, phase: str) -> Task:
        return Task.objects.create(ticket=self.ticket, session=Session.objects.create(ticket=self.ticket), phase=phase)

    def _bundle(self, phase: str, worktree: Path) -> list[str]:
        return resolve_skill_bundle(
            phase=phase,
            overlay_skill_metadata=TeatreeOverlay().metadata.get_skill_metadata(),
            worktree_path=worktree,
            stage_skills=[],
        )

    def _prepare(self, phase: str, worktree: Path) -> PreparedRun:
        dispatch = DispatchHarness(
            harness=SimpleNamespace(capabilities=HarnessCapabilities()),
            name="codex_app_server",
            provider=None,
            model=None,
            route_candidate_index=None,
        )
        preflight = Preflight(stage_skills=[], dispatch=dispatch, skills=self._bundle(phase, worktree))
        return prepare_run(self._task(phase), preflight, phase=phase, handoff=None, credential=DispatchCredential())

    def test_dispatch_requests_no_phantom_skill(self) -> None:
        for phase in _DISPATCHED_PHASES:
            with self.subTest(phase=phase):
                assurance = self._prepare(phase, _TEATREE_CHECKOUT).skill_assurance

                assert assurance["missing"] == []
                assert "teatree" not in assurance["requested"]
                assert "skills" not in assurance["requested"]

    def test_the_overlay_skill_path_itself_delivers_internals_outside_a_checkout(self) -> None:
        outside = Path(self.enterContext(tempfile.TemporaryDirectory()))
        assert SkillLoadingPolicy.detect_internals_skill(outside) == []

        assurance = self._prepare("coding", outside).skill_assurance

        assert "internals" in assurance["requested"]
        assert assurance["missing"] == []

    def test_codex_review_embeds_internals_once_when_detector_and_overlay_both_name_it(self) -> None:
        phase = "codex_reviewing"
        context = build_system_context(self._task(phase), skills=self._bundle(phase, _TEATREE_CHECKOUT))

        assert context.count("--- SKILL: internals ---") == 1

    def test_coding_stack_load_block_lists_internals_once(self) -> None:
        prompt = self._prepare("coding", _TEATREE_CHECKOUT).prompt

        assert prompt.splitlines().count("  - /internals") == 1
