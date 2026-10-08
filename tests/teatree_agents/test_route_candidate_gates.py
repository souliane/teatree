"""The per-candidate gates of an ordered route: the scalar model floor and a pinned provider."""

from django.test import TestCase

from teatree.agents.harness_dispatch import DispatchHarness, resolve_dispatch_harness
from teatree.config import AgentHarnessProvider
from teatree.config.agent_spawn import AgentConfig, AgentRouteCandidate
from teatree.core.models import Session, Task
from tests.factories import planned_ticket
from tests.teatree_agents._route_fakes import CLAUDE_LIKE, CLAUDE_SDK, register_stub_harnesses, routed_by


class TestRouteCandidateGates(TestCase):
    def setUp(self) -> None:
        register_stub_harnesses(self, CLAUDE_LIKE)
        ticket = planned_ticket()
        self.task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="coding")

    def _resolve(self, config: AgentConfig, *skills: str) -> DispatchHarness:
        with routed_by(config):
            return resolve_dispatch_harness(self.task, phase="coding", skills=list(skills))

    def test_a_candidate_below_a_loaded_skills_model_floor_is_skipped(self) -> None:
        config = AgentConfig(
            skill_models={
                "floor-skill": "opus",
                "route-skill": (AgentRouteCandidate(CLAUDE_LIKE, "haiku"), AgentRouteCandidate(CLAUDE_LIKE, "opus")),
            }
        )

        dispatch = self._resolve(config, "route-skill", "floor-skill")

        assert dispatch.route_candidate_index == 1
        assert dispatch.rejected[0].reason == "model is below a loaded skill's scalar floor"

    def test_a_candidate_naming_a_provider_keeps_it_on_the_dispatch(self) -> None:
        candidate = AgentRouteCandidate(CLAUDE_SDK, "opus", provider="subscription_oauth")

        dispatch = self._resolve(AgentConfig(skill_models={"route-skill": (candidate,)}), "route-skill")

        assert dispatch.provider is AgentHarnessProvider.SUBSCRIPTION_OAUTH
        assert dispatch.availability_provider == "subscription_oauth"
