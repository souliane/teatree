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
        # §17.6 candidate 13: a `reviewing` visit requires an explicit
        # independent reviewer --agent-id.
        call_command("lifecycle", "visit-phase", str(ticket.pk), "review", agent_id="cold-reviewer")
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
            result = cast(
                "str",
                call_command("lifecycle", "visit-phase", str(ticket.pk), "review", agent_id="cold-reviewer"),
            )

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
        session.visit_phase("testing")  # reviewing missing (#837: retro not gated)

        result = _check_shipping_gate(ticket)
        assert result is not None
        assert result["allowed"] is False
        assert "reviewing" in result["missing"]
        assert "retro" not in result["missing"]
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

    FSM-advancing ``visit-phase`` forks a fresh session by design. The
    shipping gate's single source of truth is therefore the UNION of
    phase data across all of the ticket's sessions — not the latest
    session alone.
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
        # `reviewing` never recorded on ANY session (#837: retro not gated).

        result = _check_shipping_gate(ticket)
        assert result is not None
        assert result["allowed"] is False
        assert result["missing"] == ["reviewing"]
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.STARTED

    def test_same_agent_across_sessions_no_longer_blocks(self) -> None:
        # #833: the conflicting pair recorded by the SAME agent_id on
        # DIFFERENT sessions no longer trips a gate failure — there is no
        # agent_id inference. Phases present ⇒ pass.
        ticket = _ticket(state=Ticket.State.STARTED)
        s1 = Session.objects.create(ticket=ticket, agent_id="same-agent")
        s1.visit_phase("coding", agent_id="same-agent")
        s1.visit_phase("testing", agent_id="same-agent")
        s2 = Session.objects.create(ticket=ticket, agent_id="same-agent")
        s2.visit_phase("reviewing", agent_id="same-agent")
        s2.visit_phase("retro", agent_id="same-agent")

        assert _check_shipping_gate(ticket) is None
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEWED

    def test_gate_passes_distinct_agents_across_sessions(self) -> None:
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
            patch.object(pr_command, "validate_pr_metadata", return_value=None),
        ):
            result = cast("dict[str, object]", call_command("pr", "create", str(ticket.pk)))

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SHIPPED
        assert result["ticket_id"] == ticket.pk
        assert result["state"] == Ticket.State.SHIPPED
        assert result["queued"] is True
        assert "QUEUED, not performed" in result["warning"]

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
            patch.object(pr_command, "validate_pr_metadata", return_value=None),
        ):
            result = cast(
                "dict[str, object]",
                call_command("pr", "create", str(ticket.pk), "--title", "Custom PR title"),
            )

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SHIPPED
        assert result["ticket_id"] == ticket.pk
        assert result["state"] == Ticket.State.SHIPPED
        assert result["queued"] is True
        assert "QUEUED, not performed" in result["warning"]
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
        assert result["missing"] == ["testing", "reviewing"]
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

    def test_pr_create_skip_validation_from_non_reviewed_reconciles_then_ships(self) -> None:
        # #748: --skip-validation is the user-authorized attestation
        # substitute, so the FSM follows the authorization: it walks
        # STARTED -> REVIEWED via reconcile_reviewed and ship() becomes
        # legal (never a raw TransitionNotAllowed; never a structurally
        # impossible ship). Async default => queued, no structured gate
        # failure.
        ticket = _ticket(state=Ticket.State.STARTED)
        self._worktree(ticket)
        result = cast(
            "dict[str, object]",
            call_command("pr", "create", str(ticket.pk), "--skip-validation"),
        )
        assert result.get("allowed") is not False, result
        assert "error" not in result
        ticket.refresh_from_db()
        assert ticket.state in {Ticket.State.SHIPPED, Ticket.State.REVIEWED}


class TestCliVisitPhaseRecordsAuditTrail(TestCase):
    def test_cli_visit_phase_records_into_phase_visits_keyed_by_session_identity(self) -> None:
        # CLI `visit-phase` threads the session identity into the
        # phase_visits audit trail (symmetric with the loop path).
        ticket = _ticket(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="cli-actor")
        call_command("lifecycle", "visit-phase", str(ticket.pk), "review", agent_id="cold-reviewer")

        session.refresh_from_db()
        assert "reviewing" in session.visited_phases
        assert "reviewing" in session.phase_visits
        # §17.6 candidate 13: the explicit reviewer id is recorded (not the
        # maker session identity), so the attestation names who reviewed.
        assert session.phase_visits["reviewing"]["agent_id"] == "cold-reviewer"

    def test_cli_recorded_phases_present_does_not_block_gate(self) -> None:
        # #833: phases present ⇒ gate passes. §17.6 candidate 13 additionally
        # requires the `reviewing` visit to name an explicit independent
        # reviewer — the maker phases keep the session identity, reviewing
        # carries the distinct reviewer id.
        ticket = _ticket(state=Ticket.State.REVIEWED)  # no FSM auto-scheduling
        session = Session.objects.create(ticket=ticket, agent_id="same-agent")
        call_command("lifecycle", "visit-phase", str(ticket.pk), "code")
        call_command("lifecycle", "visit-phase", str(ticket.pk), "test")
        call_command("lifecycle", "visit-phase", str(ticket.pk), "review", agent_id="cold-reviewer")

        session.refresh_from_db()
        assert session.phase_visits["coding"]["agent_id"] == "same-agent"
        assert session.phase_visits["reviewing"]["agent_id"] == "cold-reviewer"
        session.check_gate("reviewing")  # no raise

    def test_cli_distinct_agents_also_pass(self) -> None:
        ticket = _ticket(state=Ticket.State.REVIEWED)
        session = Session.objects.create(ticket=ticket, agent_id="checker")
        session.visit_phase("coding", agent_id="maker")
        session.visit_phase("testing", agent_id="maker")
        call_command("lifecycle", "visit-phase", str(ticket.pk), "review", agent_id="cold-reviewer")

        session.refresh_from_db()
        assert session.phase_visits["coding"]["agent_id"] == "maker"
        assert session.phase_visits["reviewing"]["agent_id"] == "cold-reviewer"
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
