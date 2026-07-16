"""Regression tests for ``Task._apply_phase_transition`` (#1000).

The #998/#999 orphan sweep can mark a second reviewing task COMPLETED on a
reviewer-role ticket that already advanced to DELIVERED. Without a state
guard on the ``phase == "reviewing" and role == REVIEWER`` branch the FSM
raises ``TransitionNotAllowed`` and the loop tick crashes.
"""

import pytest
from django.test import TestCase
from django_fsm import TransitionNotAllowed

from teatree.core.models import Session, Task, Ticket


class TestApplyPhaseTransitionGuardsTerminalReviewer(TestCase):
    """#1000: reviewer-ticket already in DELIVERED must not re-fire the FSM."""

    def test_completed_reviewing_task_on_delivered_ticket_no_ops(self) -> None:
        # Reviewer-role ticket already advanced through review and is now
        # DELIVERED (terminal). The #999 orphan sweep then completes a
        # second reviewing task on the same ticket.
        ticket = Ticket.objects.create(
            overlay="test",
            role=Ticket.Role.REVIEWER,
            state=Ticket.State.DELIVERED,
        )
        session = Session.objects.create(ticket=ticket, agent_id="t")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            phase="reviewing",
            status=Task.Status.COMPLETED,
        )

        # Pre-#1000 this raised TransitionNotAllowed and crashed the tick.
        fired = task._apply_phase_transition()

        ticket.refresh_from_db()
        assert fired is False, "no transition should fire on a terminal-state reviewer ticket"
        assert ticket.state == Ticket.State.DELIVERED, f"ticket state must remain DELIVERED, got {ticket.state}"

    def test_reviewer_ticket_in_source_state_still_advances(self) -> None:
        # Guard must not regress the happy path: a reviewer-role ticket
        # still in a source state of mark_reviewed_externally() (here
        # NOT_STARTED, the lowest state on the source list) must advance
        # to DELIVERED when its reviewing task completes.
        ticket = Ticket.objects.create(
            overlay="test",
            role=Ticket.Role.REVIEWER,
            state=Ticket.State.NOT_STARTED,
        )
        session = Session.objects.create(ticket=ticket, agent_id="t")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            phase="reviewing",
            status=Task.Status.COMPLETED,
        )

        fired = task._apply_phase_transition()

        ticket.refresh_from_db()
        assert fired is True, "reviewer ticket in a source state must advance"
        assert ticket.state == Ticket.State.DELIVERED

    def test_mark_reviewed_externally_still_raises_when_called_directly_on_delivered(self) -> None:
        # Sanity: the guard lives in _apply_phase_transition, not in the
        # FSM. Calling the transition directly on a DELIVERED ticket
        # still raises — proving the guard is what protects the loop.
        ticket = Ticket.objects.create(
            overlay="test",
            role=Ticket.Role.REVIEWER,
            state=Ticket.State.DELIVERED,
        )
        session = Session.objects.create(ticket=ticket, agent_id="t")
        Task.objects.create(
            ticket=ticket,
            session=session,
            phase="reviewing",
            status=Task.Status.COMPLETED,
        )

        with pytest.raises(TransitionNotAllowed):
            ticket.mark_reviewed_externally()


class TestApplyPhaseTransitionChainsParentTask(TestCase):
    """PR-12: the completing task is threaded as ``parent_task`` of the next phase.

    A phase-boundary brief is warm only if the next-phase task chains back to
    the task that just finished — ``agents.prompt._parent_result_summary``
    reads ``task.parent_task``'s last attempt. Before this the FSM transition
    scheduled the next phase with ``parent_task=None`` (a cold brief).
    """

    def _author_ticket(self, state: str) -> Ticket:
        return Ticket.objects.create(overlay="test", role=Ticket.Role.AUTHOR, state=state)

    def _completed_task(self, ticket: Ticket, phase: str) -> Task:
        session = Session.objects.create(ticket=ticket, agent_id=phase)
        return Task.objects.create(ticket=ticket, session=session, phase=phase, status=Task.Status.COMPLETED)

    def test_coding_completion_chains_testing_task_to_the_coder(self) -> None:
        ticket = self._author_ticket(Ticket.State.PLANNED)
        coding_task = self._completed_task(ticket, "coding")

        assert coding_task._apply_phase_transition() is True

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.CODED
        testing = Task.objects.get(ticket=ticket, phase="testing")
        assert testing.parent_task_id == coding_task.pk

    def test_testing_completion_chains_review_task_to_the_tester(self) -> None:
        ticket = self._author_ticket(Ticket.State.CODED)
        testing_task = self._completed_task(ticket, "testing")

        assert testing_task._apply_phase_transition() is True

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.TESTED
        review = Task.objects.get(ticket=ticket, phase="reviewing")
        assert review.parent_task_id == testing_task.pk


class TestApplyPhaseTransitionAutoImplement(TestCase):
    """#10: an auto-implement author ticket advances on coding completion.

    ``persistence._handle_orchestrator`` schedules a ``coding`` task directly on
    a fresh NOT_STARTED author ticket, so the completion cannot match the
    PLANNED-source ``code()`` guard. Before ``code_direct`` this no-opped
    silently — tickets 35/36 spent budget on coding yet advanced NOTHING.
    """

    def _completed_coding_task(self, ticket: Ticket) -> Task:
        session = Session.objects.create(ticket=ticket, agent_id="coding")
        return Task.objects.create(ticket=ticket, session=session, phase="coding", status=Task.Status.COMPLETED)

    def test_coding_completion_on_not_started_auto_implement_advances_to_coded(self) -> None:
        from teatree.core.models.auto_implement import mark_auto_implement  # noqa: PLC0415

        ticket = Ticket.objects.create(overlay="test", role=Ticket.Role.AUTHOR, state=Ticket.State.NOT_STARTED)
        mark_auto_implement(ticket)
        coding_task = self._completed_coding_task(ticket)

        fired = coding_task._apply_phase_transition()

        ticket.refresh_from_db()
        assert fired is True, "an auto-implement ticket must advance on coding completion, not silently no-op"
        assert ticket.state == Ticket.State.CODED
        testing = Task.objects.get(ticket=ticket, phase="testing")
        assert testing.parent_task_id == coding_task.pk

    def test_coding_completion_on_unmarked_early_ticket_does_not_advance(self) -> None:
        # Behavior preservation: without the marker, code_direct is unreachable —
        # a plain NOT_STARTED author ticket's coding completion must NOT advance
        # (it escalates instead; see TestApplyPhaseTransitionEscalation).
        ticket = Ticket.objects.create(overlay="test", role=Ticket.Role.AUTHOR, state=Ticket.State.NOT_STARTED)
        coding_task = self._completed_coding_task(ticket)

        fired = coding_task._apply_phase_transition()

        ticket.refresh_from_db()
        assert fired is False
        assert ticket.state == Ticket.State.NOT_STARTED


class TestApplyPhaseTransitionEscalation(TestCase):
    """#10 invariant: an FSM lifecycle transition must never fail silently.

    When no guard matches AND the ticket is behind the phase's target (a genuine
    wedge, not an idempotent replay), a durable ``DeferredQuestion`` is recorded
    so the operator is told, instead of the old silent ``return False``.
    """

    def _completed_task(self, ticket: Ticket, phase: str) -> Task:
        session = Session.objects.create(ticket=ticket, agent_id=phase)
        return Task.objects.create(ticket=ticket, session=session, phase=phase, status=Task.Status.COMPLETED)

    def test_wedged_coding_completion_records_a_deferred_question(self) -> None:
        from teatree.core.models.deferred_question import DeferredQuestion  # noqa: PLC0415

        ticket = Ticket.objects.create(overlay="test", role=Ticket.Role.AUTHOR, state=Ticket.State.NOT_STARTED)
        coding_task = self._completed_task(ticket, "coding")

        fired = coding_task._apply_phase_transition()

        assert fired is False
        pending = DeferredQuestion.pending()
        assert pending.count() == 1, "a genuine FSM wedge must escalate, never silently drop"
        assert "FSM wedge" in pending.first().question

    def test_wedge_escalation_is_deduped_across_replays(self) -> None:
        from teatree.core.models.deferred_question import DeferredQuestion  # noqa: PLC0415

        ticket = Ticket.objects.create(overlay="test", role=Ticket.Role.AUTHOR, state=Ticket.State.NOT_STARTED)
        coding_task = self._completed_task(ticket, "coding")

        coding_task._apply_phase_transition()
        coding_task._apply_phase_transition()

        assert DeferredQuestion.pending().count() == 1, "an at-least-once replay must not flood the question queue"

    def test_idempotent_replay_past_target_does_not_escalate(self) -> None:
        from teatree.core.models.deferred_question import DeferredQuestion  # noqa: PLC0415

        # A coding task completing on a ticket already advanced to TESTED (past
        # coding's CODED target) is an idempotent replay, not a wedge.
        ticket = Ticket.objects.create(overlay="test", role=Ticket.Role.AUTHOR, state=Ticket.State.TESTED)
        coding_task = self._completed_task(ticket, "coding")

        fired = coding_task._apply_phase_transition()

        assert fired is False
        assert DeferredQuestion.pending().count() == 0, "an idempotent replay must not escalate"

    def test_free_form_phase_does_not_escalate(self) -> None:
        from teatree.core.models.deferred_question import DeferredQuestion  # noqa: PLC0415

        # A non-lifecycle phase (no FSM target) legitimately no-ops.
        ticket = Ticket.objects.create(overlay="test", role=Ticket.Role.AUTHOR, state=Ticket.State.NOT_STARTED)
        task = self._completed_task(ticket, "architectural_review")

        fired = task._apply_phase_transition()

        assert fired is False
        assert DeferredQuestion.pending().count() == 0
