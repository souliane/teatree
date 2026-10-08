"""A route reached through a skill a phase merely loads never captures a read-only phase (#1769 rule R)."""

from django.test import TestCase
from django.utils import timezone

from teatree.agents.harness_dispatch import DispatchHarness, resolve_dispatch_harness
from teatree.config.agent_spawn import AgentConfig
from teatree.core.models import Session, Task, TaskAttempt
from tests.factories import planned_ticket
from tests.teatree_agents._route_fakes import CLAUDE_SDK, MANAGED, register_stub_harnesses, route_config, routed_by


class TestRouteReviewCapture(TestCase):
    def setUp(self) -> None:
        register_stub_harnesses(self, MANAGED)
        self.ticket = planned_ticket()

    def _resolve(self, config: AgentConfig, phase: str, *skills: str) -> DispatchHarness:
        task = Task.objects.create(ticket=self.ticket, session=Session.objects.create(ticket=self.ticket), phase=phase)
        with routed_by(config):
            return resolve_dispatch_harness(task, phase=phase, skills=list(skills))

    def test_reviews_that_load_the_code_skill_resolve_no_route(self) -> None:
        config = route_config("code", CLAUDE_SDK, MANAGED)

        for phase in ("reviewing", "critic_reviewing"):
            with self.subTest(phase=phase):
                dispatch = self._resolve(config, phase, "review", "code")

                assert dispatch.route_candidate_index is None
                assert (dispatch.name, dispatch.model, dispatch.effort) == (CLAUDE_SDK, None, None)

    def test_a_route_keyed_by_the_phases_own_skill_still_applies_to_it(self) -> None:
        dispatch = self._resolve(route_config("review", CLAUDE_SDK), "reviewing", "review", "code")

        assert (dispatch.name, dispatch.route_candidate_index, dispatch.route_source_skill) == (CLAUDE_SDK, 0, "review")

    def test_a_write_phase_is_still_routed_through_a_skill_it_merely_loads(self) -> None:
        dispatch = self._resolve(route_config("route-skill", MANAGED), "coding", "route-skill")

        assert (dispatch.name, dispatch.route_candidate_index) == (MANAGED, 0)

    def test_a_debug_route_listing_codex_never_resolves_it_for_a_bughunt(self) -> None:
        dispatch = self._resolve(route_config("debug", MANAGED, CLAUDE_SDK), "bughunt", "debug")

        assert (dispatch.name, dispatch.route_candidate_index) == (CLAUDE_SDK, 1)
        assert "bars write tools" in str(dispatch.rejected[-1])

    def test_an_ordinary_coding_failure_does_not_move_the_task_to_codex(self) -> None:
        task = Task.objects.create(
            ticket=self.ticket, session=Session.objects.create(ticket=self.ticket), phase="coding"
        )
        TaskAttempt.objects.create(
            task=task,
            ended_at=timezone.now(),
            exit_code=0,
            error="missing required evidence for phase 'coding': one of [files_modified]",
            route_candidate_index=0,
            route_source_skill="code",
        )

        with routed_by(route_config("code", CLAUDE_SDK, MANAGED)):
            dispatch = resolve_dispatch_harness(task, phase="coding", skills=["code"])

        assert (dispatch.name, dispatch.route_candidate_index) == (CLAUDE_SDK, 0)
