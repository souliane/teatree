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

import pytest
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


class TestShippingGateCrossSessionUnion(TestCase):
    """The required phases may be scattered across the ticket's sessions.

    FSM-advancing ``visit-phase`` forks a fresh session by design
    (bias-free maker≠checker). The shipping gate's single source of truth
    is therefore the UNION of phase data across all of the ticket's
    sessions — not the latest session alone.
    """

    def test_gate_passes_when_required_phases_scattered_across_sessions(self) -> None:
        ticket = _ticket(state=Ticket.State.STARTED)
        s1 = Session.objects.create(ticket=ticket, agent_id="maker")
        s1.visit_phase("testing", agent_id="maker")
        s2 = Session.objects.create(ticket=ticket, agent_id="checker")
        s2.visit_phase("reviewing", agent_id="checker")
        s3 = Session.objects.create(ticket=ticket, agent_id="retro-actor")
        s3.visit_phase("retro", agent_id="retro-actor")

        # No single session has all three, but the union does.
        assert _check_shipping_gate(ticket) is None
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEWED

    def test_gate_still_blocks_when_a_required_phase_is_genuinely_missing(self) -> None:
        ticket = _ticket(state=Ticket.State.STARTED)
        s1 = Session.objects.create(ticket=ticket, agent_id="maker")
        s1.visit_phase("testing", agent_id="maker")
        s2 = Session.objects.create(ticket=ticket, agent_id="checker")
        s2.visit_phase("reviewing", agent_id="checker")
        # `retro` never recorded on ANY session.

        result = _check_shipping_gate(ticket)
        assert result is not None
        assert result["allowed"] is False
        assert result["missing"] == ["retro"]
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.STARTED

    def test_maker_checker_still_blocks_same_agent_across_sessions(self) -> None:
        # The conflicting pair (coding, reviewing) recorded by the SAME
        # agent_id but on DIFFERENT sessions must still trip maker≠checker
        # once the union is evaluated — integrity preserved, not weakened.
        ticket = _ticket(state=Ticket.State.STARTED)
        s1 = Session.objects.create(ticket=ticket, agent_id="same-agent")
        s1.visit_phase("coding", agent_id="same-agent")
        s1.visit_phase("testing", agent_id="same-agent")
        s2 = Session.objects.create(ticket=ticket, agent_id="same-agent")
        s2.visit_phase("reviewing", agent_id="same-agent")
        s2.visit_phase("retro", agent_id="same-agent")

        result = _check_shipping_gate(ticket)
        assert result is not None
        assert result["allowed"] is False
        assert "Maker≠checker violation" in result["error"]
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.STARTED

    def test_maker_checker_passes_distinct_agents_across_sessions(self) -> None:
        ticket = _ticket(state=Ticket.State.STARTED)
        s1 = Session.objects.create(ticket=ticket, agent_id="maker")
        s1.visit_phase("coding", agent_id="maker")
        s1.visit_phase("testing", agent_id="maker")
        s2 = Session.objects.create(ticket=ticket, agent_id="checker")
        s2.visit_phase("reviewing", agent_id="checker")
        s2.visit_phase("retro", agent_id="checker")

        assert _check_shipping_gate(ticket) is None
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEWED


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

    def test_pr_create_title_override_is_persisted_on_ship(self) -> None:
        # The --title override path (now in _enqueue_ship) records
        # pr_title_override on ticket.extra so the ship worker reads it.
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
            result = cast(
                "dict[str, object]",
                call_command("pr", "create", str(ticket.pk), "--title", "Custom PR title"),
            )

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SHIPPED
        assert result == {"ticket_id": ticket.pk, "state": Ticket.State.SHIPPED}
        assert ticket.extra["pr_title_override"] == "Custom PR title"


class TestShippingGateNoSession(TestCase):
    def test_gate_blocks_with_structured_failure_when_no_session(self) -> None:
        # No session => no attested work; the gate must return a structured
        # failure, NOT None (which would let `ship()` raise a raw
        # TransitionNotAllowed from a non-REVIEWED state).
        ticket = _ticket(state=Ticket.State.STARTED)
        assert ticket.sessions.count() == 0

        result = _check_shipping_gate(ticket)
        assert result is not None
        assert result["allowed"] is False
        assert result["missing"] == ["testing", "reviewing", "retro"]
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.STARTED  # not advanced


class TestPrCreateNeverRaisesEvenOnNoSessionOrSkipValidation(TestCase):
    def _worktree(self, ticket: Ticket) -> None:
        Worktree.objects.create(
            ticket=ticket,
            overlay="test",
            repo_path="/tmp/backend",
            branch="feature-branch",
            extra={"worktree_path": "/tmp/backend"},
        )

    def test_pr_create_no_session_returns_structured_failure_not_raise(self) -> None:
        ticket = _ticket(state=Ticket.State.STARTED)
        self._worktree(ticket)
        result = cast("dict[str, object]", call_command("pr", "create", str(ticket.pk)))
        assert result["allowed"] is False
        assert "missing" in result
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.STARTED

    def test_pr_create_skip_validation_from_non_reviewed_returns_structured_failure(self) -> None:
        # --skip-validation bypasses the gate check; the raw ticket.ship()
        # from STARTED would raise TransitionNotAllowed. The invariant
        # "pr create never raises a raw TransitionNotAllowed" must still hold.
        ticket = _ticket(state=Ticket.State.STARTED)
        self._worktree(ticket)
        result = cast(
            "dict[str, object]",
            call_command("pr", "create", str(ticket.pk), "--skip-validation"),
        )
        assert result["allowed"] is False
        assert "error" in result
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.STARTED


class TestCliVisitPhaseFeedsMakerChecker(TestCase):
    def test_cli_visit_phase_records_into_phase_visits_keyed_by_session_identity(self) -> None:
        # The nit: CLI `visit-phase` recorded phases WITHOUT an agent_id, so
        # they never landed in `phase_visits` and `_check_maker_checker`
        # silently `continue`d past them. After the fix the CLI must thread
        # the session identity (symmetric with the loop path) so the phase
        # IS recorded in phase_visits and the maker≠checker check can see it.
        ticket = _ticket(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="cli-actor")
        call_command("lifecycle", "visit-phase", str(ticket.pk), "review")

        session.refresh_from_db()
        assert "reviewing" in session.visited_phases
        # The bug was: reviewing recorded in visited_phases but NOT in
        # phase_visits, so maker≠checker skipped it. Now it lands here.
        assert "reviewing" in session.phase_visits
        assert session.phase_visits["reviewing"]["agent_id"] == "cli-actor"

    def test_cli_recorded_conflicting_phases_same_session_trip_maker_checker(self) -> None:
        # When both conflicting phases land on the SAME session via the CLI
        # (e.g. out-of-order / free-form recording that does not auto-schedule
        # a fresh review session), the same identity for coding+reviewing
        # must now trip maker≠checker rather than silently bypassing it.
        from teatree.core.models.errors import QualityGateError  # noqa: PLC0415

        ticket = _ticket(state=Ticket.State.REVIEWED)  # no FSM auto-scheduling
        session = Session.objects.create(ticket=ticket, agent_id="same-agent")
        # Record the prerequisite + both conflicting phases via the CLI on
        # this single session (REVIEWED state => no schedule_* spawns a new
        # session, so all three land on `session`).
        call_command("lifecycle", "visit-phase", str(ticket.pk), "code")
        call_command("lifecycle", "visit-phase", str(ticket.pk), "test")
        call_command("lifecycle", "visit-phase", str(ticket.pk), "review")

        session.refresh_from_db()
        assert session.phase_visits["coding"]["agent_id"] == "same-agent"
        assert session.phase_visits["reviewing"]["agent_id"] == "same-agent"
        with pytest.raises(QualityGateError, match="Maker≠checker violation"):
            session.check_gate("reviewing")

    def test_cli_distinct_agents_pass_maker_checker(self) -> None:
        # Honest path: a distinct reviewing identity does NOT trip the check.
        ticket = _ticket(state=Ticket.State.REVIEWED)
        session = Session.objects.create(ticket=ticket, agent_id="checker")
        session.visit_phase("coding", agent_id="maker")
        session.visit_phase("testing", agent_id="maker")
        call_command("lifecycle", "visit-phase", str(ticket.pk), "review")

        session.refresh_from_db()
        assert session.phase_visits["coding"]["agent_id"] == "maker"
        assert session.phase_visits["reviewing"]["agent_id"] == "checker"
        # Different agents across the pair — gate passes (no raise).
        session.check_gate("reviewing")


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
