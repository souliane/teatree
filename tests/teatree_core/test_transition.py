from typing import cast

from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import Session, Task, Ticket, TicketTransition
from teatree.core.selectors import build_ticket_lifecycle_mermaid


def _advance_ticket_to_tested(ticket: Ticket) -> None:
    ticket.scope(issue_url="https://example.com/issues/99", variant="acme", repos=["repo"])
    ticket.save()
    ticket.start()
    ticket.save()
    ticket.code()
    ticket.save()
    ticket.test(passed=True)
    ticket.save()


class TestTicketTransitionAudit(TestCase):
    def test_transition_creates_audit_row(self) -> None:
        ticket = Ticket.objects.create()
        ticket.scope(issue_url="https://example.com/issues/1")
        ticket.save()

        assert TicketTransition.objects.filter(ticket=ticket).count() == 1
        t = TicketTransition.objects.get(ticket=ticket)
        assert t.from_state == "not_started"
        assert t.to_state == "scoped"
        assert t.triggered_by == "scope"

    def test_full_lifecycle_creates_all_transitions(self) -> None:
        ticket = Ticket.objects.create()
        _advance_ticket_to_tested(ticket)

        transitions = list(TicketTransition.objects.filter(ticket=ticket).order_by("created_at"))
        states = [(t.from_state, t.to_state) for t in transitions]

        assert states == [
            ("not_started", "scoped"),
            ("scoped", "started"),
            ("started", "coded"),
            ("coded", "tested"),
        ]

    def test_session_fk_populated_from_latest_session(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="test-agent")

        ticket.scope(issue_url="https://example.com/issues/2")
        ticket.save()

        t = TicketTransition.objects.get(ticket=ticket)
        assert t.session == session

    def test_session_fk_null_when_no_session(self) -> None:
        ticket = Ticket.objects.create()
        ticket.scope(issue_url="https://example.com/issues/3")
        ticket.save()

        t = TicketTransition.objects.get(ticket=ticket)
        assert t.session is None

    def test_rework_creates_transition_back_to_started(self) -> None:
        ticket = Ticket.objects.create()
        _advance_ticket_to_tested(ticket)

        ticket.rework()
        ticket.save()

        last = TicketTransition.objects.filter(ticket=ticket).order_by("created_at").last()
        assert last.from_state == "tested"
        assert last.to_state == "started"
        assert last.triggered_by == "rework"

    def test_review_transition_recorded(self) -> None:
        ticket = Ticket.objects.create()
        _advance_ticket_to_tested(ticket)

        task = ticket.tasks.get(phase="reviewing", status=Task.Status.PENDING)
        task.claim(claimed_by="test-worker")
        task.complete()

        ticket.refresh_from_db()
        last = TicketTransition.objects.filter(ticket=ticket).order_by("created_at").last()
        assert last.from_state == "tested"
        assert last.to_state == "reviewed"
        assert last.triggered_by == "review"

    def test_str_representation(self) -> None:
        ticket = Ticket.objects.create()
        ticket.scope(issue_url="https://example.com/issues/4")
        ticket.save()

        t = TicketTransition.objects.get(ticket=ticket)
        assert str(t) == "not_started → scoped (scope)"


class TestTicketLifecycleMermaid(TestCase):
    def test_mermaid_contains_all_transitions(self) -> None:
        ticket = Ticket.objects.create()
        _advance_ticket_to_tested(ticket)

        mermaid = build_ticket_lifecycle_mermaid(ticket.pk)

        assert "stateDiagram-v2" in mermaid
        assert "not_started --> scoped: scope()" in mermaid
        assert "scoped --> started: start()" in mermaid
        assert "started --> coded: code()" in mermaid
        assert "coded --> tested: test()" in mermaid

    def test_mermaid_includes_session_id(self) -> None:
        ticket = Ticket.objects.create()
        Session.objects.create(ticket=ticket, agent_id="agent-1")

        ticket.scope(issue_url="https://example.com/issues/5")
        ticket.save()

        mermaid = build_ticket_lifecycle_mermaid(ticket.pk)
        assert "scope() S" in mermaid

    def test_mermaid_highlights_current_state(self) -> None:
        ticket = Ticket.objects.create()
        ticket.scope(issue_url="https://example.com/issues/6")
        ticket.save()

        mermaid = build_ticket_lifecycle_mermaid(ticket.pk)
        assert "note right of scoped: current" in mermaid

    def test_mermaid_empty_transitions(self) -> None:
        ticket = Ticket.objects.create()

        mermaid = build_ticket_lifecycle_mermaid(ticket.pk)
        assert "stateDiagram-v2" in mermaid
        assert "note right of not_started: current" in mermaid


class TestLifecycleDiagramTicketFlag(TestCase):
    def test_diagram_with_ticket_flag(self) -> None:
        ticket = Ticket.objects.create()
        ticket.scope(issue_url="https://example.com/issues/7")
        ticket.save()

        result = call_command("lifecycle", "diagram", ticket=ticket.pk)
        assert "not_started --> scoped: scope()" in result


class TestScheduleReviewInSession(TestCase):
    def test_creates_task_in_existing_session(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="coding-agent")

        task = ticket.schedule_review_in_session(session)

        assert task.session == session
        assert task.phase == "reviewing"
        assert task.ticket == ticket
        # Should NOT create a new session
        assert Session.objects.filter(ticket=ticket).count() == 1


class TestCheckGatesStructured(TestCase):
    def test_check_gates_returns_missing_phases(self) -> None:
        ticket = Ticket.objects.create()
        Session.objects.create(ticket=ticket)

        result = cast("dict[str, object]", call_command("pr", "check-gates", ticket.pk, target_phase="shipping"))

        assert result["allowed"] is False
        assert "reviewing" in cast("list[str]", result["missing"])
        assert "testing" in cast("list[str]", result["missing"])

    def test_check_gates_passes_when_phases_visited(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        session.visit_phase("testing")
        session.visit_phase("reviewing")
        session.visit_phase("retro")

        result = cast("dict[str, object]", call_command("pr", "check-gates", ticket.pk, target_phase="shipping"))

        assert result["allowed"] is True


class TestVisitPhaseCommand(TestCase):
    def test_visit_phase_marks_session(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="agent")

        call_command("lifecycle", "visit-phase", ticket.pk, "reviewing")

        session.refresh_from_db()
        assert session.has_visited("reviewing")

    def test_visit_phase_creates_session_if_none(self) -> None:
        ticket = Ticket.objects.create()

        call_command("lifecycle", "visit-phase", ticket.pk, "testing")

        session = ticket.sessions.first()
        assert session is not None
        assert session.has_visited("testing")
