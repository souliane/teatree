"""Regression tests for ``Task._apply_phase_transition`` (#1000).

The #998/#999 orphan sweep can mark a second reviewing task COMPLETED on a
reviewer-role ticket that already advanced to REVIEW_POSTED, and a re-review of
a new head SHA does the same deliberately. Either way the tick must survive and
the ticket must stay REVIEW_POSTED — ``mark_reviewed_externally`` self-loops there
rather than raising ``TransitionNotAllowed``, the same shape #1431 gave its
sibling ``mark_review_no_action``.
"""

from unittest.mock import patch

from django.test import TestCase

from teatree.core.models import Session, Task, Ticket


class TestApplyPhaseTransitionGuardsTerminalReviewer(TestCase):
    """#1000: completing a reviewing task on a REVIEW_POSTED reviewer ticket never crashes the tick.

    The invariant #1000 protects is "the tick survives and the ticket stays
    REVIEW_POSTED", not "the FSM refuses". The refusal was only ever the
    mechanism, and it was the *crash source*: a second reviewing task on a
    terminal ticket raised ``TransitionNotAllowed`` and the guard in
    ``_apply_phase_transition`` had to skip it. ``mark_reviewed_externally``
    now self-loops on REVIEW_POSTED — the same fix #1431 applied to its sibling
    ``mark_review_no_action`` in the same file — so the completion is an
    idempotent no-op at the FSM level and the re-review of a new head SHA can
    re-stamp the reviewed-at record.
    """

    def test_completed_reviewing_task_on_terminal_ticket_stays_review_posted(self) -> None:
        # Reviewer-role ticket already advanced through review and is now
        # REVIEW_POSTED (terminal). The #999 orphan sweep then completes a
        # second reviewing task on the same ticket.
        ticket = Ticket.objects.create(
            overlay="test",
            role=Ticket.Role.REVIEWER,
            state=Ticket.State.REVIEW_POSTED,
        )
        session = Session.objects.create(ticket=ticket, agent_id="t")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            phase="reviewing",
            status=Task.Status.COMPLETED,
        )

        # Pre-#1000 this raised TransitionNotAllowed and crashed the tick.
        task._apply_phase_transition()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.REVIEW_POSTED, f"ticket state must remain REVIEW_POSTED, got {ticket.state}"

    def test_reviewer_ticket_in_source_state_still_advances(self) -> None:
        # Guard must not regress the happy path: a reviewer-role ticket
        # still in a source state of mark_reviewed_externally() (here
        # NOT_STARTED, the lowest state on the source list) must advance
        # to REVIEW_POSTED when its reviewing task completes.
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
        assert ticket.state == Ticket.State.REVIEW_POSTED

    def test_mark_reviewed_externally_is_idempotent_when_called_directly_on_terminal(self) -> None:
        # The crash is gone at the source: the transition self-loops on
        # REVIEW_POSTED instead of raising, so neither the orphan sweep nor a
        # re-review of a new head SHA can wedge the tick.
        ticket = Ticket.objects.create(
            overlay="test",
            role=Ticket.Role.REVIEWER,
            state=Ticket.State.REVIEW_POSTED,
        )
        session = Session.objects.create(ticket=ticket, agent_id="t")
        Task.objects.create(
            ticket=ticket,
            session=session,
            phase="reviewing",
            status=Task.Status.COMPLETED,
        )

        ticket.mark_reviewed_externally()

        assert ticket.state == Ticket.State.REVIEW_POSTED


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


class TestApplyPhaseTransitionReReadsStateUnderLock(TestCase):
    """The state read + guard + advance happen inside ONE atomic, re-reading under a lock.

    Reading the ticket state OUTSIDE the write transaction (the previous
    ``refresh_from_db`` before ``transaction.atomic()``) let two concurrent
    completions both read the same stale state and both fire the transition —
    two ``schedule_*`` tasks + two Sessions (double-fire). The fix re-reads the
    ticket under ``select_for_update`` INSIDE the atomic, so on the production
    BEGIN IMMEDIATE backend the second completion sees the first's committed
    state and no-ops.
    """

    def _completed_coding_task(self, ticket: Ticket) -> Task:
        session = Session.objects.create(ticket=ticket, agent_id="coding")
        return Task.objects.create(ticket=ticket, session=session, phase="coding", status=Task.Status.COMPLETED)

    def test_ticket_is_re_read_under_select_for_update_inside_atomic(self) -> None:
        from django.db import connection  # noqa: PLC0415
        from django.db.models.query import QuerySet  # noqa: PLC0415

        ticket = Ticket.objects.create(overlay="test", role=Ticket.Role.AUTHOR, state=Ticket.State.PLANNED)
        coding_task = self._completed_coding_task(ticket)

        real_sfu = QuerySet.select_for_update
        locked_reads: list[bool] = []

        def sfu_spy(qs: QuerySet, *args: object, **kwargs: object) -> QuerySet:
            if qs.model is Ticket:
                locked_reads.append(connection.in_atomic_block)
            return real_sfu(qs, *args, **kwargs)

        with patch.object(QuerySet, "select_for_update", sfu_spy):
            fired = coding_task._apply_phase_transition()

        assert fired is True
        # The ticket IS re-read under a row lock (the previous shape read state
        # via a bare refresh_from_db OUTSIDE the atomic — zero Ticket
        # select_for_update in this path), and every such read is inside the
        # atomic block.
        assert locked_reads, "the ticket must be re-read under select_for_update inside the transition"
        assert all(locked_reads), "every ticket lock-read must happen inside the atomic block"


class TestApplyPhaseTransitionUnshippableReviewDisposition(TestCase):
    """A REVIEWED ticket with no shippable diff gets an explicit terminal disposition.

    ``review()`` lands REVIEWED and stamps ``shipping_skipped`` when there is no
    shippable diff. Without a disposition the ticket rests at REVIEWED forever,
    holding its issue-implementer marker + WIP slot. It is now auto-ignored
    (terminal → releases both), preserving the reason in ``extra``.
    """

    def _completed_reviewing_task(self, ticket: Ticket) -> Task:
        session = Session.objects.create(ticket=ticket, agent_id="reviewing")
        return Task.objects.create(ticket=ticket, session=session, phase="reviewing", status=Task.Status.COMPLETED)

    def test_no_shippable_diff_auto_ignores(self) -> None:
        ticket = Ticket.objects.create(overlay="test", role=Ticket.Role.AUTHOR, state=Ticket.State.TESTED)
        review_task = self._completed_reviewing_task(ticket)

        with patch.object(Ticket, "has_shippable_diff", return_value=False):
            fired = review_task._apply_phase_transition()

        ticket.refresh_from_db()
        assert fired is True
        assert ticket.state == Ticket.State.IGNORED, "a no-shippable-diff review must reach a terminal disposition"
        assert ticket.extra.get("shipping_skipped")
        assert ticket.extra.get("ignored_from") == Ticket.State.REVIEWED

    def test_shippable_diff_stays_reviewed_and_schedules_shipping(self) -> None:
        ticket = Ticket.objects.create(overlay="test", role=Ticket.Role.AUTHOR, state=Ticket.State.TESTED)
        review_task = self._completed_reviewing_task(ticket)

        with patch.object(Ticket, "has_shippable_diff", return_value=True):
            fired = review_task._apply_phase_transition()

        ticket.refresh_from_db()
        assert fired is True
        assert ticket.state == Ticket.State.REVIEWED, "a shippable review must NOT be auto-ignored"
        assert Task.objects.filter(ticket=ticket, phase="shipping").exists()


class TestTransitionSourceStatesDerivation(TestCase):
    """The mark_reviewed_externally source set is DERIVED from the FSM, not hand-listed."""

    def test_derives_declared_source_states(self) -> None:
        from teatree.core.models.task_phase_disposition import transition_source_states  # noqa: PLC0415

        assert transition_source_states("mark_reviewed_externally") == {
            Ticket.State.NOT_STARTED,
            Ticket.State.SCOPED,
            Ticket.State.STARTED,
            Ticket.State.PLANNED,
            Ticket.State.CODED,
            Ticket.State.TESTED,
            Ticket.State.REVIEWED,
            # The re-review self-loop: a new head SHA schedules a second review
            # on an already-REVIEW_POSTED reviewer ticket, whose completion must
            # be able to re-stamp the reviewed-at record.
            Ticket.State.REVIEW_POSTED,
        }

    def test_wildcard_transition_yields_empty_set(self) -> None:
        from teatree.core.models.task_phase_disposition import transition_source_states  # noqa: PLC0415

        # teardown is not a Ticket transition; an unknown name yields nothing.
        assert transition_source_states("no_such_transition") == set()
