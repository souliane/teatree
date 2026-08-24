"""Auto-drain stale repair-loop escalation questions when their subject reconciles (#3692).

A repair-loop escalation (``repair-halt`` / ``repair-stall`` / ``repair-cap``)
records a durable ``DeferredQuestion`` asking the owner how a halted phase should
proceed. When the subject ticket subsequently reaches a terminal state (its PR
merged, delivered, or ignored) the question is MOOT — the loop will never retry
that phase again, so the only possible answer is "ignore". Left pending, these
moot rows bury the one live question the owner needs to answer.

The generalised drain (:mod:`teatree.loop.question_drain`) subsumes the reconcile and
keeps its predicate verbatim: a row is drained ONLY when EVERY ticket that raised it is
terminal. A row with even one still-live subject —
or whose subject cannot be determined — is KEPT. Dropping a question whose subject
is still live is the failure mode these tests pin against.
"""

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import Session, Task, TaskAttempt, Ticket
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.loop.question_drain import drain_pending_questions
from teatree.loop.tick_recovery import _reap_stale_task_claims
from teatree.loop.transient_requeue import HALT_STAMP, escalation_marker, requeue_transient_failed


def _failed_task(*, phase: str = "coding", state: str = Ticket.State.STARTED) -> Task:
    ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=state)
    session = Session.objects.create(ticket=ticket, agent_id=phase)
    return Task.objects.create(ticket=ticket, session=session, phase=phase, status=Task.Status.FAILED)


def _add_failed_attempt(task: Task, *, error: str) -> None:
    TaskAttempt.objects.create(
        task=task,
        ended_at=timezone.now(),
        exit_code=1,
        error=error,
    )
    Task.objects.filter(pk=task.pk).update(status=Task.Status.FAILED)


def _escalated_halt_task() -> Task:
    """Drive a real ``repair-halt`` escalation: two identical failures halt + queue a question."""
    task = _failed_task()
    _add_failed_attempt(task, error="result_error: no terminal ResultMessage")
    _add_failed_attempt(task, error="result_error: no terminal ResultMessage")
    assert requeue_transient_failed() == 0
    task.refresh_from_db()
    assert HALT_STAMP in task.execution_reason
    assert DeferredQuestion.objects.filter(dedupe_marker__startswith="repair-halt:").count() == 1
    return task


class TestRepairHaltReconcile(TestCase):
    def test_merged_subject_drains_the_halt_question(self) -> None:
        task = _escalated_halt_task()
        Ticket.objects.filter(pk=task.ticket_id).update(state=Ticket.State.MERGED)

        resolved = drain_pending_questions().drained

        assert resolved == 1
        question = DeferredQuestion.objects.get(dedupe_marker__startswith="repair-halt:")
        assert question.status == DeferredQuestion.STATUS_DISMISSED
        assert question.resolved_via == DeferredQuestion.ResolvedVia.STALE
        assert question.dismissed_reason

    def test_live_subject_question_is_never_touched(self) -> None:
        # The over-resolve guard: the subject ticket is still STARTED — the halt is a
        # genuine live question, so the reconcile must leave it pending untouched.
        _escalated_halt_task()

        resolved = drain_pending_questions().drained

        assert resolved == 0
        question = DeferredQuestion.objects.get(dedupe_marker__startswith="repair-halt:")
        assert question.status == DeferredQuestion.STATUS_PENDING

    def test_one_live_subject_among_merged_keeps_the_shared_question(self) -> None:
        # A fingerprint-keyed ``repair-halt`` marker collapses several tickets onto ONE
        # question. When one subject merged but another is still live, the shared row must
        # stay pending — the live ticket still needs the answer.
        live = _escalated_halt_task()
        merged = _failed_task()
        _add_failed_attempt(merged, error="result_error: no terminal ResultMessage")
        _add_failed_attempt(merged, error="result_error: no terminal ResultMessage")
        assert requeue_transient_failed() == 0  # same fingerprint ⇒ same marker, no 2nd question
        assert DeferredQuestion.objects.filter(dedupe_marker__startswith="repair-halt:").count() == 1
        Ticket.objects.filter(pk=merged.ticket_id).update(state=Ticket.State.MERGED)

        resolved = drain_pending_questions().drained

        assert resolved == 0  # `live`'s ticket is still STARTED
        question = DeferredQuestion.objects.get(dedupe_marker__startswith="repair-halt:")
        assert question.status == DeferredQuestion.STATUS_PENDING
        assert live.ticket.state == Ticket.State.STARTED

    def test_all_subjects_merged_drains_the_shared_question(self) -> None:
        first = _escalated_halt_task()
        second = _failed_task()
        _add_failed_attempt(second, error="result_error: no terminal ResultMessage")
        _add_failed_attempt(second, error="result_error: no terminal ResultMessage")
        assert requeue_transient_failed() == 0
        Ticket.objects.filter(pk__in=[first.ticket_id, second.ticket_id]).update(state=Ticket.State.MERGED)

        resolved = drain_pending_questions().drained

        assert resolved == 1
        question = DeferredQuestion.objects.get(dedupe_marker__startswith="repair-halt:")
        assert question.status == DeferredQuestion.STATUS_DISMISSED

    def test_ticket_keyed_stall_marker_drains_on_terminal_subject(self) -> None:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.MERGED)
        DeferredQuestion.record(
            "Repair-loop stall on ticket (phase 'coding'): identical failures.",
            dedupe_marker=f"repair-stall:{ticket.pk}:coding",
            audience=DeferredQuestion.Audience.INTERNAL,
        )

        resolved = drain_pending_questions().drained

        assert resolved == 1
        assert DeferredQuestion.objects.get(dedupe_marker__startswith="repair-stall:").status == (
            DeferredQuestion.STATUS_DISMISSED
        )

    def test_ticket_keyed_cap_marker_kept_on_live_subject(self) -> None:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.STARTED)
        DeferredQuestion.record(
            "Repair-loop cap on ticket (phase 'coding'): iteration cap hit.",
            dedupe_marker=f"repair-cap:{ticket.pk}:coding",
            audience=DeferredQuestion.Audience.INTERNAL,
        )

        resolved = drain_pending_questions().drained

        assert resolved == 0
        assert DeferredQuestion.objects.get(dedupe_marker__startswith="repair-cap:").status == (
            DeferredQuestion.STATUS_PENDING
        )

    def test_ticket_keyed_marker_missing_ticket_is_kept(self) -> None:
        # A marker whose subject ticket no longer exists cannot be proven moot → kept.
        DeferredQuestion.record(
            "Repair-loop stall on a vanished ticket.",
            dedupe_marker="repair-stall:999999:coding",
            audience=DeferredQuestion.Audience.INTERNAL,
        )

        assert drain_pending_questions().drained == 0
        assert DeferredQuestion.objects.filter(dismissed_at__isnull=True).count() == 1

    def test_non_repair_question_is_never_touched(self) -> None:
        DeferredQuestion.record("A real owner decision", dedupe_marker="attachment-hold:5")

        assert drain_pending_questions().drained == 0
        assert DeferredQuestion.objects.get(dedupe_marker="attachment-hold:5").status == (
            DeferredQuestion.STATUS_PENDING
        )

    def test_already_resolved_question_is_not_re_counted(self) -> None:
        task = _escalated_halt_task()
        Ticket.objects.filter(pk=task.ticket_id).update(state=Ticket.State.MERGED)

        assert drain_pending_questions().drained == 1
        # Idempotent: a second pass finds it already dismissed and drains nothing.
        assert drain_pending_questions().drained == 0

    def test_reconcile_keys_on_the_same_marker_the_escalation_writes(self) -> None:
        # The reconcile re-derives a parked task's marker to map it back to its question,
        # so the derivation MUST equal the dedupe_marker the escalation recorded.
        task = _escalated_halt_task()
        question = DeferredQuestion.objects.get(dedupe_marker__startswith="repair-halt:")
        assert escalation_marker(task) == question.dedupe_marker

    def test_reconcile_is_wired_into_tick_recovery(self) -> None:
        task = _escalated_halt_task()
        Ticket.objects.filter(pk=task.ticket_id).update(state=Ticket.State.MERGED)

        _reap_stale_task_claims()

        question = DeferredQuestion.objects.get(dedupe_marker__startswith="repair-halt:")
        assert question.status == DeferredQuestion.STATUS_DISMISSED
