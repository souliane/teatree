"""A freshly created loop-dispatched phase task lands in the runtime's own lane.

The save-time hook was named ``_default_loop_dispatched_to_interactive`` and its
docstring called itself "the single chokepoint for phase tasks default to interactive".
Both were stale: the shipped ``agent_runtime`` is ``headless``, so the hook is a no-op
on every shipped install and a reader concluded the opposite of what happens.
"""

import pytest
from django.test import TestCase

from teatree.config import AgentRuntime, get_effective_settings
from teatree.core.modelkit.phases import SUBAGENT_BY_PHASE
from teatree.core.models import Task
from tests.factories import SessionFactory, TicketFactory

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_ROLE, _PHASE = next(iter(SUBAGENT_BY_PHASE))


class TestLoopDispatchedLaneRouting(TestCase):
    def _phase_task(self) -> Task:
        ticket = TicketFactory.create(role=_ROLE)
        return Task.objects.create(
            ticket=ticket,
            session=SessionFactory.create(ticket=ticket),
            phase=_PHASE,
            execution_target=Task.ExecutionTarget.HEADLESS,
        )

    def test_the_shipped_runtime_leaves_a_phase_task_headless(self) -> None:
        assert get_effective_settings().agent_runtime is AgentRuntime.HEADLESS, "control: shipped default"
        assert self._phase_task().execution_target == Task.ExecutionTarget.HEADLESS

    def test_the_hook_is_named_for_the_lane_it_routes(self) -> None:
        assert hasattr(Task, "_route_loop_dispatched_lane")
        assert not hasattr(Task, "_default_loop_dispatched_to_interactive")
