"""A candidate that ran and failed through its own fault is not dispatched again while another one can run (#5153)."""

from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

import teatree.agents.runner as runner_mod
from teatree.agents.harness_dispatch import DispatchHarness, resolve_dispatch_harness
from teatree.agents.runner import TaskUsage, run_agent
from teatree.config.agent_spawn import AgentConfig
from teatree.core.models import Session, Task, TaskAttempt
from teatree.types import SkillMetadata
from tests.factories import planned_ticket
from tests.teatree_agents._route_fakes import CLAUDE_LIKE, MANAGED, register_stub_harnesses, route_config, routed_by

_LANDING = "landing_unverified: no new commit on feat-x — HEAD has not advanced past the base"
_ROUTE = route_config("route-skill", MANAGED, CLAUDE_LIKE)


class _RouteProgressCase(TestCase):
    phase = "coding"

    def setUp(self) -> None:
        register_stub_harnesses(self, MANAGED, CLAUDE_LIKE)
        ticket = planned_ticket()
        self.task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase=self.phase)

    def _record(self, index: int | None, error: str, *, exit_code: int = 0) -> TaskAttempt:
        return TaskAttempt.objects.create(
            task=self.task,
            ended_at=timezone.now(),
            exit_code=exit_code,
            error=error,
            route_candidate_index=index,
            route_source_skill="route-skill",
        )

    def _resolve(self, config: AgentConfig = _ROUTE) -> DispatchHarness:
        with routed_by(config):
            return resolve_dispatch_harness(self.task, phase=self.phase, skills=["route-skill"])


class TestRouteProgress(_RouteProgressCase):
    def test_refusal_dispatches_next_candidate(self) -> None:
        refused = self._record(0, _LANDING)

        dispatch = self._resolve()

        assert (dispatch.name, dispatch.route_candidate_index) == (CLAUDE_LIKE, 1)
        assert f"attempt {refused.pk}" in str(dispatch.rejected[-1])

    def test_a_crash_dispatches_next_candidate(self) -> None:
        self._record(0, "Traceback (most recent call last):\nRuntimeError: boom", exit_code=1)

        assert self._resolve().route_candidate_index == 1

    def test_the_last_passing_candidate_is_retried_once_every_candidate_failed(self) -> None:
        self._record(0, _LANDING)
        self._record(1, _LANDING)

        assert self._resolve().route_candidate_index == 1

    def test_a_single_candidate_route_resolves_index_zero_every_time(self) -> None:
        self._record(0, _LANDING)

        assert self._resolve(route_config("route-skill", MANAGED)).route_candidate_index == 0

    def test_an_ordinary_failure_keeps_the_task_on_candidate_zero(self) -> None:
        for error in (
            "missing required evidence for phase 'coding': one of [files_modified]",
            "agent_abandoned: the agent failed the task without a reason",
            "outage_death: API Error 529",
        ):
            with self.subTest(error=error):
                self._record(0, error)

                assert self._resolve().route_candidate_index == 0

    def test_a_task_that_failed_only_on_the_second_candidate_returns_to_the_first_once_it_passes(self) -> None:
        self._record(1, _LANDING)

        assert self._resolve().route_candidate_index == 0

    def test_eligibility_decides_before_failure_does(self) -> None:
        self._record(0, _LANDING)

        dispatch = self._resolve(route_config("route-skill", MANAGED, "not_registered"))

        assert (dispatch.name, dispatch.route_candidate_index) == (MANAGED, 0)

    def test_a_park_gives_the_failed_candidate_another_turn(self) -> None:
        self._record(0, _LANDING)
        self._record(None, "limit_parked: subscription_session window")

        assert self._resolve().route_candidate_index == 0


class TestRunAgentDispatchesTheNextCandidate(_RouteProgressCase):
    phase = "debugging"

    def test_the_recorded_attempt_carries_the_index_and_the_failed_attempt_as_its_reason(self) -> None:
        refused = self._record(0, _LANDING)

        with (
            routed_by(_ROUTE),
            patch("teatree.agents.runner_skill_staging.resolve_skill_bundle", return_value=["route-skill"]),
            patch.object(Task, "renew_lease", lambda self, **_kw: None),
            patch.object(runner_mod.TaskUsage, "for_task", classmethod(lambda cls, task: TaskUsage(0, 0.0))),
        ):
            final = run_agent(self.task, phase=self.phase, overlay_skill_metadata=SkillMetadata())

        assert final.route_candidate_index == 1
        assert final.selected_harness == CLAUDE_LIKE
        assert f"attempt {refused.pk}" in final.fallback_reason
