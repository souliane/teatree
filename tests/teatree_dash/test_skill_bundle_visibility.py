"""An empty skill bundle must read as a FAULT, not as an absence of information (#3886).

The skill bundle is the biggest single determinant of how an agent behaves in a phase,
and it is assembled from sources that can each silently contribute nothing — the phase
frontmatter, cwd-driven detection, the transitive ``requires:`` chain against a cached
index, the overlay's companions. A cold cache or a detector that does not fire degrades
the bundle quietly and the dispatch still looks normal.

``TaskAttempt.skills_loaded`` already records the resolved bundle, and the ticket drawer
already renders it — but ONLY when it is non-empty, so the one case worth seeing rendered
as nothing at all. These pin the opposite: a headless dispatch that recorded no bundle
says so, wherever it is listed. An interactive attempt is exempt by construction — it
runs inside the operator's own session and never resolves a bundle to record.
"""

from django.test import TestCase
from django.urls import reverse

from teatree.core.models.task import Task
from teatree.core.models.task_attempt import TaskAttempt
from teatree.core.models.ticket import Ticket
from teatree.dash.sessions import build_session_index
from teatree.dash.skills import skill_bundle
from tests.factories import TaskFactory, TicketFactory

State = Ticket.State
_LOOPBACK = {"REMOTE_ADDR": "127.0.0.1"}


def _attempt(**kwargs: object) -> TaskAttempt:
    ticket = TicketFactory(state=State.STARTED)
    task = TaskFactory(ticket=ticket, phase="coding")
    defaults = {"execution_target": Task.ExecutionTarget.HEADLESS, "agent_session_id": "sess-skills"}
    return TaskAttempt.objects.create(task=task, **{**defaults, **kwargs})


class TheSharedRuleTestCase(TestCase):
    """One rule decides what every surface renders, so they cannot disagree."""

    def test_a_recorded_bundle_is_returned_and_is_no_fault(self) -> None:
        assert skill_bundle(_attempt(skills_loaded=["t3:code"])) == (("t3:code",), False)

    def test_an_empty_bundle_on_a_headless_dispatch_is_a_fault(self) -> None:
        assert skill_bundle(_attempt(skills_loaded=[])) == ((), True)

    def test_an_interactive_attempt_is_exempt(self) -> None:
        attempt = _attempt(execution_target=Task.ExecutionTarget.INTERACTIVE, skills_loaded=[])
        assert skill_bundle(attempt) == ((), False)


class SessionIndexCarriesTheBundleTestCase(TestCase):
    def test_a_session_row_lists_the_skills_its_dispatch_recorded(self) -> None:
        _attempt(skills_loaded=["t3:code", "t3:rules"])
        assert build_session_index()[0].skills == ("t3:code", "t3:rules")

    def test_a_headless_session_with_no_bundle_is_flagged(self) -> None:
        _attempt(skills_loaded=[])
        assert build_session_index()[0].skills_fault

    def test_an_interactive_session_with_no_bundle_is_not_flagged(self) -> None:
        _attempt(execution_target=Task.ExecutionTarget.INTERACTIVE, skills_loaded=[])
        assert not build_session_index()[0].skills_fault

    def test_the_sessions_page_shows_the_bundle(self) -> None:
        _attempt(skills_loaded=["t3:code"])
        body = self.client.get(reverse("dash:sessions"), **_LOOPBACK).content.decode()
        assert "t3:code" in body

    def test_the_sessions_page_states_the_fault_rather_than_leaving_it_blank(self) -> None:
        _attempt(skills_loaded=[])
        body = self.client.get(reverse("dash:sessions"), **_LOOPBACK).content.decode()
        assert "no skills recorded" in body


class TicketDrawerStatesTheFaultTestCase(TestCase):
    def test_a_headless_attempt_with_no_bundle_says_so_in_the_drawer(self) -> None:
        attempt = _attempt(skills_loaded=[])
        url = reverse("dash:ticket_drawer", args=[attempt.task.ticket_id])
        body = self.client.get(url, **_LOOPBACK).content.decode()
        assert "no skills recorded" in body

    def test_an_attempt_with_a_bundle_lists_it_and_is_not_faulted(self) -> None:
        attempt = _attempt(skills_loaded=["t3:code"])
        url = reverse("dash:ticket_drawer", args=[attempt.task.ticket_id])
        body = self.client.get(url, **_LOOPBACK).content.decode()
        assert "t3:code" in body
        assert "no skills recorded" not in body
