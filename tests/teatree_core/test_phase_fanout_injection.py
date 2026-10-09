"""Route-invariant fan-out directives from the phase registry (teatree#2229).

The interactive dispatch payload and headless composer must both include the
directive for every registered pair without a configuration opt-in.
"""

import json
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from teatree.agents.prompt import build_system_context
from teatree.core.models import Task, Ticket
from tests._pr_open_state_stub import mint_open_pr_review
from tests.factories import planned_ticket


class _FanoutDispatchTest(TestCase):
    def _reviewer_task(self, *, url: str = "https://example.com/pr/1") -> Task:
        ticket = Ticket.objects.create(
            overlay="acme",
            issue_url=url,
            role=Ticket.Role.REVIEWER,
            extra={"reviewed_sha": "x"},
        )
        return mint_open_pr_review(ticket)

    def _planning_task(self, *, url: str = "https://example.com/issues/9") -> Task:
        ticket = Ticket.objects.create(overlay="acme", issue_url=url, role=Ticket.Role.AUTHOR)
        return ticket.schedule_planning()

    def _coding_task(self, *, url: str = "https://example.com/issues/7") -> Task:
        ticket = planned_ticket(overlay="acme", issue_url=url, role=Ticket.Role.AUTHOR)
        return ticket.schedule_coding()

    def _entry(self) -> dict:
        stdout = StringIO()
        call_command("loop_dispatch", "claim-next", "--json", stdout=stdout)
        return json.loads(stdout.getvalue())[0]


class TestRouteInvarianceAgainstRealComposer(_FanoutDispatchTest):
    """Assert against the real ``claim-next`` dispatch payload."""

    def test_payload_always_carries_a_fanout_directive_key(self) -> None:
        self._reviewer_task()
        assert "fanout_directive" in self._entry()

    def test_registered_reviewing_renders_default_directive(self) -> None:
        self._reviewer_task()
        directive = self._entry()["fanout_directive"]
        assert "adversarial-verify" in directive
        assert "N=3" in directive

    def test_registered_planning_renders_default_directive(self) -> None:
        self._planning_task()
        directive = self._entry()["fanout_directive"]
        assert "judge-panel" in directive
        assert "N=3" in directive

    def test_unregistered_coding_renders_empty_directive(self) -> None:
        self._coding_task()
        assert self._entry()["fanout_directive"] == ""


class TestClaimNextCarriesFanoutDirective(_FanoutDispatchTest):
    def test_claim_next_payload_carries_the_directive(self) -> None:
        self._reviewer_task()
        stdout = StringIO()
        call_command("loop_dispatch", "claim-next", "--json", stdout=stdout)
        entry = json.loads(stdout.getvalue())[0]
        assert "adversarial-verify" in entry["fanout_directive"]


class TestHeadlessParity(_FanoutDispatchTest):
    """The headless composer must carry the same registry directive."""

    def _context(self, task: Task) -> str:
        return build_system_context(task, skills=[], lifecycle_skill="t3:review")

    def test_reviewing_context_carries_default_directive(self) -> None:
        task = self._reviewer_task()
        context = self._context(task)
        assert "adversarial-verify" in context
        assert "N=3" in context

    def test_planning_context_carries_default_directive(self) -> None:
        task = self._planning_task()
        context = self._context(task)
        assert "judge-panel" in context
        assert "N=3" in context
