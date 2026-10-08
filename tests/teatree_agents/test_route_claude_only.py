"""A ticket marked ``extra['claude_only']`` is never dispatched to a harness other than claude_sdk."""

from django.test import TestCase
from django.utils import timezone

from teatree.agents.harness_dispatch import DispatchHarness, candidate_selection, resolve_dispatch_harness
from teatree.agents.harness_registry import HarnessBuildContext
from teatree.core.models import Session, Task, TaskAttempt
from teatree.core.models.types import validated_ticket_extra
from tests.factories import planned_ticket
from tests.teatree_agents._route_fakes import CLAUDE_SDK, MANAGED, register_stub_harnesses, route_config, routed_by

_PYDANTIC_LIKE = "pydantic_like"
_REASON = "ticket is claude_only"


class TestClaudeOnlyTicket(TestCase):
    def setUp(self) -> None:
        register_stub_harnesses(self, MANAGED, _PYDANTIC_LIKE)

    def _task(self, **extra: object) -> Task:
        ticket = planned_ticket(extra=extra)
        return Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="coding")

    def _resolve(self, task: Task, *harnesses: str) -> DispatchHarness:
        with routed_by(route_config("route-skill", *harnesses)):
            return resolve_dispatch_harness(task, phase="coding", skills=["route-skill"])

    def test_a_skill_route_rejects_every_other_harness_and_uses_claude(self) -> None:
        dispatch = self._resolve(self._task(claude_only=True), MANAGED, _PYDANTIC_LIKE, CLAUDE_SDK)

        assert (dispatch.name, dispatch.route_candidate_index) == (CLAUDE_SDK, 2)
        assert [r.reason for r in dispatch.rejected] == [_REASON, _REASON]

    def test_a_ticket_without_the_key_reaches_codex_when_claude_has_failed(self) -> None:
        task = self._task()
        TaskAttempt.objects.create(
            task=task,
            ended_at=timezone.now(),
            exit_code=0,
            error="landing_unverified: no new commit on feat-x — HEAD has not advanced past the base",
            route_candidate_index=0,
            route_source_skill="route-skill",
        )

        assert self._resolve(task, CLAUDE_SDK, MANAGED).name == MANAGED

    def test_the_overlay_phase_candidate_path_obeys_it_too(self) -> None:
        task = self._task(claude_only=True)
        context = HarnessBuildContext(task=task, phase="coding", overlay="")

        selection = candidate_selection(context, [MANAGED, CLAUDE_SDK])

        assert selection is not None
        assert selection.spec.name == CLAUDE_SDK
        assert [str(r) for r in selection.rejected] == [f"{MANAGED}: {_REASON}"]


class TestClaudeOnlySurvivesTheTicketTransitions(TestCase):
    def test_the_key_is_declared_so_a_transition_does_not_strip_it(self) -> None:
        assert validated_ticket_extra({"claude_only": True, "unrelated": 1}) == {"claude_only": True}
