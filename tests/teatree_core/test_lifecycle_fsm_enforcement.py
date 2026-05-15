"""FSM enforcement: leaks fixed + single source of truth (#694).

Covers the three streams. Stream 1: ``visit-phase`` accepts issue numbers
and short phase names, and fails loudly. Stream 2: the shipping gate
reconciles ``ticket.state`` from ``visited_phases`` so ``pr create`` never
raises a raw ``TransitionNotAllowed``. Stream 3: the loop/task path records
the visited phase so the gate's single source of truth is fed without a
separate CLI call.
"""

from typing import cast
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from teatree.core.management.commands import pr as pr_command
from teatree.core.management.commands.pr import _check_shipping_gate
from teatree.core.models import Session, Ticket, Worktree
from teatree.core.overlay_loader import reset_overlay_cache
from tests.teatree_core.conftest import CommandOverlay


def _ticket(**kw: object) -> Ticket:
    return Ticket.objects.create(overlay="test", **kw)


class TestVisitPhaseIdentifierResolution(TestCase):
    def test_visit_phase_accepts_issue_number(self) -> None:
        ticket = _ticket(issue_url="https://github.com/souliane/teatree/issues/694")
        # Pass the forge issue number, not the DB pk — the #694 bug.
        result = cast("str", call_command("lifecycle", "visit-phase", "694", "code"))

        assert ticket.sessions.count() == 1
        session = ticket.sessions.first()
        assert "coding" in session.visited_phases
        assert "694" not in str(session.visited_phases)  # normalized, not raw
        assert "coding" in result

    def test_visit_phase_accepts_issue_url(self) -> None:
        ticket = _ticket(issue_url="https://github.com/souliane/teatree/issues/700")
        call_command(
            "lifecycle",
            "visit-phase",
            "https://github.com/souliane/teatree/issues/700",
            "test",
        )
        session = ticket.sessions.first()
        assert "testing" in session.visited_phases


class TestVisitPhaseVocabulary(TestCase):
    def test_short_name_advances_fsm(self) -> None:
        ticket = _ticket(state=Ticket.State.NOT_STARTED)
        # Skills emit the short verb "scope", not the gerund "scoping".
        call_command("lifecycle", "visit-phase", str(ticket.pk), "scope")
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SCOPED

    def test_gerund_still_advances_fsm(self) -> None:
        ticket = _ticket(state=Ticket.State.NOT_STARTED)
        call_command("lifecycle", "visit-phase", str(ticket.pk), "scoping")
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SCOPED

    def test_review_short_name_recorded_canonically(self) -> None:
        ticket = _ticket()
        call_command("lifecycle", "visit-phase", str(ticket.pk), "review")
        session = ticket.sessions.first()
        assert "reviewing" in session.visited_phases

    def test_free_form_phase_recorded_without_fsm_advance(self) -> None:
        # A phase with no associated FSM transition still records (so the
        # session stays the single source of truth) and reports state.
        ticket = _ticket(state=Ticket.State.STARTED)
        result = cast("str", call_command("lifecycle", "visit-phase", str(ticket.pk), "brainstorm"))
        ticket.refresh_from_db()
        session = ticket.sessions.first()
        assert "brainstorm" in session.visited_phases
        assert ticket.state == Ticket.State.STARTED
        assert "started" in result


class TestVisitPhaseLoudFailure(TestCase):
    def test_out_of_order_transition_logs_warning_and_reports_state(self) -> None:
        ticket = _ticket(state=Ticket.State.NOT_STARTED)
        with self.assertLogs("teatree.core.management.commands.lifecycle", level="WARNING") as cm:
            result = cast("str", call_command("lifecycle", "visit-phase", str(ticket.pk), "review"))

        ticket.refresh_from_db()
        # Phase still recorded (single source of truth), FSM did NOT move.
        session = ticket.sessions.first()
        assert "reviewing" in session.visited_phases
        assert ticket.state == Ticket.State.NOT_STARTED
        # Loud, not swallowed: WARNING + visible state in the output.
        assert any("not valid" in m or "not allowed" in m.lower() for m in cm.output)
        assert "not_started" in result


class TestShippingGateReconciliation(TestCase):
    def test_gate_auto_walks_fsm_to_reviewed_when_phases_present(self) -> None:
        # The loop path advanced phases but the FSM is still STARTED
        # (the dual-source-of-truth bug). The gate must reconcile.
        ticket = _ticket(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket)
        session.visit_phase("testing")
        session.visit_phase("reviewing")
        session.visit_phase("retro")

        assert _check_shipping_gate(ticket) is None
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEWED

    def test_gate_blocks_with_missing_list_when_phases_absent(self) -> None:
        ticket = _ticket(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket)
        session.visit_phase("testing")  # reviewing + retro missing

        result = _check_shipping_gate(ticket)
        assert result is not None
        assert result["allowed"] is False
        assert "reviewing" in result["missing"]
        assert "retro" in result["missing"]
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.STARTED  # not advanced

    def test_gate_already_reviewed_is_noop(self) -> None:
        ticket = _ticket(state=Ticket.State.REVIEWED)
        session = Session.objects.create(ticket=ticket)
        session.visit_phase("testing")
        session.visit_phase("reviewing")
        session.visit_phase("retro")
        assert _check_shipping_gate(ticket) is None


class TestPrCreateNeverRaisesTransitionNotAllowed(TestCase):
    def test_pr_create_blocks_instead_of_raising_when_fsm_behind(self) -> None:
        # FSM stuck at STARTED, no phases visited — pr create must return a
        # structured gate failure, NOT raise TransitionNotAllowed.
        ticket = _ticket(state=Ticket.State.STARTED)
        Session.objects.create(ticket=ticket)
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="/tmp/backend",
            branch="feature-branch",
            extra={"worktree_path": "/tmp/backend"},
        )
        result = cast("dict[str, object]", call_command("pr", "create", str(ticket.pk)))
        assert result["allowed"] is False
        assert "missing" in result
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.STARTED

    def test_pr_create_reconciles_then_ships_when_fsm_behind_but_phases_present(self) -> None:
        # The acceptance criterion: the loop advanced phases but the FSM is
        # still STARTED. `pr create` must reconcile to REVIEWED and ship —
        # NOT raise a raw TransitionNotAllowed.
        reset_overlay_cache()
        self.addCleanup(reset_overlay_cache)
        ticket = _ticket(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, overlay="test")
        session.visit_phase("testing")
        session.visit_phase("reviewing")
        session.visit_phase("retro")
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="/tmp/backend",
            branch="feature-branch",
            extra={"worktree_path": "/tmp/backend"},
        )
        with (
            patch(
                "teatree.core.overlay_loader._discover_overlays",
                return_value={"test": CommandOverlay()},
            ),
            patch.object(pr_command, "_run_visual_qa_gate", return_value=None),
            patch.object(pr_command, "_validate_pr_metadata", return_value=None),
        ):
            result = cast("dict[str, object]", call_command("pr", "create", str(ticket.pk)))

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SHIPPED
        assert result == {"ticket_id": ticket.pk, "state": Ticket.State.SHIPPED}


class TestLoopPathRecordsVisitedPhase(TestCase):
    def test_task_completion_records_phase_on_session(self) -> None:
        # Stream 3: completing a task auto-advances the FSM *and* records the
        # visited phase, so the shipping gate's single source of truth is fed
        # without a separate `visit-phase` CLI call.
        from teatree.core.models.task import Task  # noqa: PLC0415

        ticket = _ticket(state=Ticket.State.CODED)
        session = Session.objects.create(ticket=ticket, agent_id="testing")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            phase="testing",
            execution_target=Task.ExecutionTarget.HEADLESS,
            execution_reason="test",
        )
        task.complete()

        ticket.refresh_from_db()
        session.refresh_from_db()
        assert ticket.state == Ticket.State.TESTED
        assert "testing" in session.visited_phases

    def test_phaseless_task_completion_records_nothing(self) -> None:
        # A bookkeeping task with no phase completes without polluting
        # the session's visited_phases.
        from teatree.core.models.task import Task  # noqa: PLC0415

        ticket = _ticket(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            phase="",
            execution_target=Task.ExecutionTarget.HEADLESS,
            execution_reason="bookkeeping",
        )
        task.complete()

        session.refresh_from_db()
        assert session.visited_phases == []
