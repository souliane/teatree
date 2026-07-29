# test-path: cross-cutting
"""The test-worker-budget divisor counts EVERY live headless agent (#3644 / F9).

The per-agent pytest worker count is ``cores*2 // active_agents``; the divisor
is the live-headless-agent count. The pre-fix divisor counted through
``Task.dispatchable_q()``, which selects only registered-phase pairs and so
EXCLUDED the free-form headless phases (``architectural_review`` …) that go
through this very cap — undercounting the set and handing each agent too many
workers (the melt direction). These pin that a free-form headless agent is
counted, and that the old ``dispatchable_q``-scoped count did NOT see it.
"""

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import Session, Task, Ticket

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _claimed_headless(phase: str) -> Task:
    ticket = Ticket.objects.create()
    session = Session.objects.create(ticket=ticket)
    task = Task.objects.create(
        ticket=ticket,
        session=session,
        execution_target=Task.ExecutionTarget.HEADLESS,
        status=Task.Status.PENDING,
        phase=phase,
    )
    task.claim(claimed_by="worker", lease_seconds=900)
    return task


class TestLiveHeadlessAgentCount(TestCase):
    def test_a_free_form_headless_agent_is_counted(self) -> None:
        _claimed_headless("architectural_review")
        assert Task.objects.live_headless_agent_count() == 1

    def test_an_expired_lease_is_not_in_flight(self) -> None:
        task = _claimed_headless("architectural_review")
        Task.objects.filter(pk=task.pk).update(lease_expires_at=timezone.now() - timezone.timedelta(seconds=1))
        assert Task.objects.live_headless_agent_count() == 0

    def test_the_old_dispatchable_scoped_count_undercounts_free_form_agents(self) -> None:
        # The divergence the fix closes: dispatchable_q() excludes the free-form
        # phase, so counting through it saw ZERO while a real agent was live.
        _claimed_headless("architectural_review")
        assert Task.objects.in_flight_claimed_count(Task.dispatchable_q()) == 0
        assert Task.objects.live_headless_agent_count() == 1


class TestActiveAgentCountDivisor(TestCase):
    def test_divisor_reflects_live_headless_agents(self) -> None:
        from teatree.agents.headless import _active_agent_count  # noqa: PLC0415 - deferred: local import

        _claimed_headless("architectural_review")
        _claimed_headless("architectural_review")
        assert _active_agent_count() == 2

    def test_divisor_floors_at_one_with_no_live_agents(self) -> None:
        from teatree.agents.headless import _active_agent_count  # noqa: PLC0415 - deferred: local import

        assert _active_agent_count() == 1
