"""Repair-halt owner DMs coalesce across tickets by root cause (#3671).

Repair-halt records a durable ``DeferredQuestion`` (later mirrored to one owner Slack
DM). When several tickets stall on the SAME root cause — the observed
``agent_harness_provider`` / ``agent_harness`` mismatch fired one DM per ticket — the
escalation must collapse to a SINGLE open question keyed on the root-cause fingerprint,
so one halt condition pages the owner once, not once per ticket. A genuinely different
failure still gets its own question.

The escalation marker is exercised here with a clean deterministic-refusal error (the
same fixture the sibling same-condition/distinct-condition tests use) so the test
isolates the cross-ticket dedup and does not depend on the #3665 self-repair path that
corrects some harness/provider pairs before they can escalate.
"""

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import Session, Task, TaskAttempt, Ticket
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.loop.transient_requeue import requeue_transient_failed

_SHARED_CONDITION = "missing required evidence for phase 'reviewing'"


def _failed_task_on_new_ticket(*, phase: str, error: str) -> Task:
    ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.TESTED)
    session = Session.objects.create(ticket=ticket, agent_id=phase)
    task = Task.objects.create(ticket=ticket, session=session, phase=phase, status=Task.Status.FAILED)
    TaskAttempt.objects.create(
        task=task,
        execution_target=task.execution_target,
        ended_at=timezone.now(),
        exit_code=1,
        error=error,
    )
    Task.objects.filter(pk=task.pk).update(status=Task.Status.FAILED)
    return task


class TestRepairHaltDmCoalesce(TestCase):
    def test_same_root_cause_across_tickets_collapses_to_one_question(self) -> None:
        # THE FLOOD FIX (#3671): three DISTINCT tickets stalling on the identical root
        # cause are ONE standing condition — they must collapse to a single open
        # DeferredQuestion (⇒ one owner DM), not one per ticket (the observed flood).
        for _ in range(3):
            _failed_task_on_new_ticket(phase="reviewing", error=_SHARED_CONDITION)

        requeue_transient_failed()

        assert DeferredQuestion.objects.filter(answered_at__isnull=True).count() == 1

    def test_different_root_causes_across_tickets_do_not_over_collapse(self) -> None:
        # Guard on the coalescing: two DIFFERENT failures on two tickets are two
        # conditions and must page separately, or a real second problem is hidden.
        _failed_task_on_new_ticket(phase="reviewing", error=_SHARED_CONDITION)
        _failed_task_on_new_ticket(phase="reviewing", error="AssertionError: reviewer crashed")

        requeue_transient_failed()

        assert DeferredQuestion.objects.filter(answered_at__isnull=True).count() == 2
