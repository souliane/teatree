"""A ``repair-`` marker OWNS its row's subject — no fall-through to the session (#4178).

The generalised drain added two subject sources the ``repair-`` reconcile (#3692) never
had: ``parked_task`` and a numeric ``session_id``. The hazard the generalisation creates
is that a ``repair-`` row whose marker cannot name its subject stops being undeterminable
and starts being answered by whichever source replies next — and the escalation writes
``session_id=str(task.session_id)``, so EVERY repair-halt row carries one. #3692's rule is
that an undeterminable repair subject is KEPT; these tests pin that the generalisation did
not quietly trade it away.
"""

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import Session, Task, TaskAttempt, Ticket
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.loop.question_drain import drain_pending_questions
from teatree.loop.transient_requeue import HALT_STAMP, requeue_transient_failed


def _failed_task(*, phase: str = "coding") -> Task:
    ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.STARTED)
    session = Session.objects.create(ticket=ticket, agent_id=phase)
    return Task.objects.create(ticket=ticket, session=session, phase=phase, status=Task.Status.FAILED)


def _add_failed_attempt(task: Task, *, error: str) -> None:
    TaskAttempt.objects.create(task=task, ended_at=timezone.now(), exit_code=1, error=error)
    Task.objects.filter(pk=task.pk).update(status=Task.Status.FAILED)


def _escalated_halt_task() -> Task:
    """Drive a real ``repair-halt`` escalation: two identical failures halt + queue a question."""
    task = _failed_task()
    _add_failed_attempt(task, error="result_error: no terminal ResultMessage")
    _add_failed_attempt(task, error="result_error: no terminal ResultMessage")
    assert requeue_transient_failed() == 0
    task.refresh_from_db()
    assert HALT_STAMP in task.execution_reason
    return task


def _halt_question() -> DeferredQuestion:
    return DeferredQuestion.objects.get(dedupe_marker__startswith="repair-halt:")


def _session_keyed_question(*, marker: str, ticket_state: str) -> DeferredQuestion:
    """A row whose ``session_id`` resolves to a Session on a ticket in *ticket_state*."""
    ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=ticket_state)
    session = Session.objects.create(ticket=ticket, agent_id="coding")
    return DeferredQuestion.record(
        "How should this proceed?",
        session_id=str(session.pk),
        dedupe_marker=marker,
        audience=DeferredQuestion.Audience.INTERNAL,
    )


class TestRepairMarkerOwnsItsSubject(TestCase):
    def test_halt_row_whose_parked_tasks_were_reaped_is_kept(self) -> None:
        # The escalation stamps session_id=<task session pk>, and that session's ticket is
        # the SAME ticket — so once the parked task is reaped the marker goes
        # undeterminable while the session still answers "terminal". #3692 keeps this row.
        task = _escalated_halt_task()
        question = _halt_question()
        Ticket.objects.filter(pk=task.ticket_id).update(state=Ticket.State.MERGED)
        Task.objects.filter(pk=task.pk).delete()

        assert drain_pending_questions().drained == 0
        question.refresh_from_db()
        assert question.status == DeferredQuestion.STATUS_PENDING

    def test_halt_row_with_surviving_parked_tasks_still_drains(self) -> None:
        # The #3692 semantics the fix must not narrow: a determinable, all-terminal
        # repair-halt subject is still moot and still drains.
        task = _escalated_halt_task()
        question = _halt_question()
        Ticket.objects.filter(pk=task.ticket_id).update(state=Ticket.State.MERGED)

        assert drain_pending_questions().drained == 1
        question.refresh_from_db()
        assert question.status == DeferredQuestion.STATUS_DISMISSED

    def test_halt_row_with_one_live_parked_subject_is_kept(self) -> None:
        live = _escalated_halt_task()
        merged = _failed_task()
        _add_failed_attempt(merged, error="result_error: no terminal ResultMessage")
        _add_failed_attempt(merged, error="result_error: no terminal ResultMessage")
        assert requeue_transient_failed() == 0  # same fingerprint ⇒ one shared question
        Ticket.objects.filter(pk=merged.ticket_id).update(state=Ticket.State.MERGED)

        assert drain_pending_questions().drained == 0
        assert _halt_question().status == DeferredQuestion.STATUS_PENDING
        assert live.ticket.state == Ticket.State.STARTED

    def test_ticket_keyed_marker_naming_no_ticket_is_kept(self) -> None:
        # repair-stall carries its subject pk. An unresolvable pk is undeterminable, not an
        # invitation to read the asking session's ticket instead.
        question = _session_keyed_question(marker="repair-stall:999999:coding", ticket_state=Ticket.State.MERGED)

        assert drain_pending_questions().drained == 0
        question.refresh_from_db()
        assert question.status == DeferredQuestion.STATUS_PENDING

    def test_unrecognised_repair_marker_is_kept(self) -> None:
        # A repair- marker of a kind no resolver parses is undeterminable — the same KEEP
        # main's `repair-` filter gives it, not a session-derived drain.
        question = _session_keyed_question(marker="repair-future-kind:7", ticket_state=Ticket.State.MERGED)

        assert drain_pending_questions().drained == 0
        question.refresh_from_db()
        assert question.status == DeferredQuestion.STATUS_PENDING

    def test_ticket_keyed_marker_still_drains_on_its_own_terminal_subject(self) -> None:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.MERGED)
        question = DeferredQuestion.record(
            "Repair-loop stall.",
            dedupe_marker=f"repair-stall:{ticket.pk}:coding",
            audience=DeferredQuestion.Audience.INTERNAL,
        )

        assert drain_pending_questions().drained == 1
        question.refresh_from_db()
        assert question.status == DeferredQuestion.STATUS_DISMISSED


class TestNonRepairSubjectsAreUnchanged(TestCase):
    def test_session_keyed_row_still_drains_on_a_terminal_subject(self) -> None:
        # The #4178 generalisation itself: a marker-less row IS answered by its session.
        question = _session_keyed_question(marker="", ticket_state=Ticket.State.DELIVERED)

        assert drain_pending_questions().drained == 1
        question.refresh_from_db()
        assert question.status == DeferredQuestion.STATUS_DISMISSED

    def test_harness_uuid_session_is_never_a_subject(self) -> None:
        question = DeferredQuestion.record("A real owner decision", session_id="a0e4ab27-26ec-41bc-bb72-2a140141762f")

        assert drain_pending_questions().drained == 0
        question.refresh_from_db()
        assert question.status == DeferredQuestion.STATUS_PENDING

    def test_parked_task_answers_a_markerless_row(self) -> None:
        task = _failed_task()
        Ticket.objects.filter(pk=task.ticket_id).update(state=Ticket.State.MERGED)
        question = DeferredQuestion.record("How should this park proceed?", parked_task=task)

        assert drain_pending_questions().drained == 1
        question.refresh_from_db()
        assert question.status == DeferredQuestion.STATUS_DISMISSED

    def test_non_repair_marker_falls_through_to_the_session(self) -> None:
        # The marker parse is deliberately not widened past `repair-`; a foreign marker is
        # simply not applicable, so the later sources still get their turn.
        question = _session_keyed_question(marker="attachment-hold:5", ticket_state=Ticket.State.MERGED)

        assert drain_pending_questions().drained == 1
        question.refresh_from_db()
        assert question.status == DeferredQuestion.STATUS_DISMISSED
