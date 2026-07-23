"""The headless worker runs non-agentic phases deterministically, not as an agent spawn.

``short_describe`` is a fixed text transformation over the ``Ticket`` row, so it is
routed to its own runner rather than handed a ticket-work brief its empty toolset
cannot satisfy. Every other phase resolves to ``None`` (dispatches agentically).
"""

from unittest.mock import patch

from django.test import TestCase

from teatree.core.deterministic_phases import deterministic_phase_runner, run_deterministic_phase
from teatree.core.models import Session, Task, Ticket

_SUMMARIZE = "teatree.agents.ticket_short_description._summarize"
_RUNNERS = "teatree.core.deterministic_phases._RUNNERS"


def _short_describe_task(title: str = "add dark mode toggle") -> Task:
    ticket = Ticket.objects.create(overlay="t3-teatree", extra={"issue_title": title})
    session = Session.objects.create(ticket=ticket, agent_id="short-describe")
    return Task.objects.create(ticket=ticket, session=session, phase="short_describe")


class TestDeterministicPhaseRunner(TestCase):
    def test_short_describe_resolves_to_a_runner(self) -> None:
        assert deterministic_phase_runner("short_describe") is not None

    def test_an_agentic_phase_resolves_to_none(self) -> None:
        assert deterministic_phase_runner("coding") is None

    def test_short_describe_runner_writes_the_summary(self) -> None:
        task = _short_describe_task()
        runner = deterministic_phase_runner(task.phase)
        assert runner is not None

        with patch(_SUMMARIZE, return_value="dark mode toggle"):
            outcome = runner(task)

        task.ticket.refresh_from_db()
        assert task.ticket.short_description == "dark mode toggle"
        assert str(task.ticket.pk) in outcome


class TestRunDeterministicPhase(TestCase):
    def test_agentic_phase_returns_none(self) -> None:
        task = _short_describe_task()
        task.phase = "coding"

        assert run_deterministic_phase(task) is None

    def test_success_records_a_completed_attempt(self) -> None:
        task = _short_describe_task()

        with patch(_SUMMARIZE, return_value="dark mode toggle"):
            result = run_deterministic_phase(task)

        assert result is not None
        assert result["exit_code"] == "0"
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED

    def test_a_raising_runner_records_a_failed_attempt_and_does_not_escape(self) -> None:
        task = _short_describe_task()

        def _boom(_task: Task) -> str:
            message = "kaboom"
            raise RuntimeError(message)

        with patch(_RUNNERS, {"short_describe": _boom}):
            result = run_deterministic_phase(task)

        assert result is not None
        assert result["exit_code"] == "1"
        assert "kaboom" in result["phase_error"]
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
