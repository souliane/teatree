"""A phase pinned to claude_sdk, or one that bars write tools, never runs on another harness through a route."""

from unittest.mock import patch

from django.test import TestCase

import teatree.agents.model_tiering as model_tiering_mod
from teatree.agents.harness_dispatch import DispatchHarness, resolve_dispatch_harness
from teatree.config import AgentHarness
from teatree.config.agent_spawn import AgentConfig
from teatree.core.models import Session, Task
from tests.factories import planned_ticket
from tests.teatree_agents._route_fakes import (
    CLAUDE_LIKE,
    CLAUDE_SDK,
    MANAGED,
    register_stub_harnesses,
    route_config,
    routed_by,
)


class TestRoutePinGuard(TestCase):
    def setUp(self) -> None:
        register_stub_harnesses(self, MANAGED, CLAUDE_LIKE)
        ticket = planned_ticket()
        self.session = Session.objects.create(ticket=ticket)
        self.ticket = ticket

    def _resolve(self, phase: str, skill: str) -> DispatchHarness:
        task = Task.objects.create(ticket=self.ticket, session=self.session, phase=phase)
        with routed_by(route_config(skill, MANAGED, CLAUDE_SDK)):
            return resolve_dispatch_harness(task, phase=phase, skills=[skill])

    def test_a_pinned_verification_phase_rejects_codex_naming_the_pin(self) -> None:
        for phase, skill in (("reviewing", "review"), ("testing", "test")):
            with self.subTest(phase=phase):
                dispatch = self._resolve(phase, skill)

                assert (dispatch.name, dispatch.route_candidate_index) == (CLAUDE_SDK, 1)
                assert f"phase {phase!r} is pinned to harness 'claude_sdk'" in dispatch.rejected[0].reason

    def test_a_code_route_never_changes_the_harness_of_the_same_tickets_review(self) -> None:
        coding = self._resolve("coding", "code")
        review = self._resolve("reviewing", "code")

        assert (coding.name, review.name) == (MANAGED, CLAUDE_SDK)
        assert review.route_candidate_index is None

    def test_a_harness_outside_the_managed_lane_still_runs_a_read_only_phase(self) -> None:
        task = Task.objects.create(ticket=self.ticket, session=self.session, phase="bughunt")
        with routed_by(route_config("debug", CLAUDE_LIKE)):
            dispatch = resolve_dispatch_harness(task, phase="bughunt", skills=["debug"])

        assert (dispatch.name, dispatch.route_candidate_index) == (CLAUDE_LIKE, 0)

    def test_a_phase_override_outside_the_pinned_verification_phases_leaves_a_route_alone(self) -> None:
        overrides = AgentConfig(phase_harness={"coding": AgentHarness.PYDANTIC_AI})
        task = Task.objects.create(ticket=self.ticket, session=self.session, phase="coding")
        with (
            patch.object(model_tiering_mod, "resolve_agent_config", return_value=overrides),
            routed_by(route_config("code", MANAGED, CLAUDE_SDK)),
        ):
            dispatch = resolve_dispatch_harness(task, phase="coding", skills=["code"])

        assert (dispatch.name, dispatch.route_candidate_index) == (MANAGED, 0)
