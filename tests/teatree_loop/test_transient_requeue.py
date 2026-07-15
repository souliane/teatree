"""Bounded auto-requeue of transient-FAILED tasks — the retry, hard-bounded.

A task that RETURNS a transient failure (an outage envelope, a provisioning-step
failure, an incomplete run, a coder yield that landed no commit) lands terminal
FAILED and, before this sweep, stayed there forever with no retry. The sweep
reopens it (FAILED → PENDING) so the loop resumes — but ONLY within the #2009
repair-loop budget: a phase at its iteration cap, or stalled on two identical
failures, is NOT reopened and is escalated LOUDLY via a durable
``DeferredQuestion``. A DETERMINISTIC failure (a test failure, an assertion) is
never reopened. The hardest pin: it NEVER retries endlessly.
"""

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import Session, Task, TaskAttempt, Ticket
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.repair_loop import max_phase_iterations
from teatree.loop.tick_recovery import _reap_stale_task_claims
from teatree.loop.transient_requeue import requeue_transient_failed


def _failed_task(*, phase: str = "coding", state: str = Ticket.State.STARTED) -> Task:
    ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=state)
    session = Session.objects.create(ticket=ticket, agent_id=phase)
    return Task.objects.create(ticket=ticket, session=session, phase=phase, status=Task.Status.FAILED)


def _add_failed_attempt(task: Task, *, error: str) -> None:
    TaskAttempt.objects.create(
        task=task,
        execution_target=task.execution_target,
        ended_at=timezone.now(),
        exit_code=1,
        error=error,
    )
    Task.objects.filter(pk=task.pk).update(status=Task.Status.FAILED)


class TestTransientRequeue(TestCase):
    def test_transient_failed_task_is_reopened_once(self) -> None:
        task = _failed_task()
        _add_failed_attempt(task, error="outage_death: connection refused")

        reopened = requeue_transient_failed()

        task.refresh_from_db()
        assert reopened == 1
        assert task.status == Task.Status.PENDING
        # A second pass finds it PENDING (no longer FAILED) — no double reopen.
        assert requeue_transient_failed() == 0

    def test_deterministic_failed_task_is_not_reopened(self) -> None:
        task = _failed_task()
        _add_failed_attempt(task, error="AssertionError: expected 3 got 4")

        assert requeue_transient_failed() == 0

        task.refresh_from_db()
        assert task.status == Task.Status.FAILED

    def test_failed_task_on_terminal_ticket_is_not_reopened(self) -> None:
        task = _failed_task(state=Ticket.State.SHIPPED)
        _add_failed_attempt(task, error="outage_death: connection refused")

        assert requeue_transient_failed() == 0

        task.refresh_from_db()
        assert task.status == Task.Status.FAILED

    def test_identical_double_failure_is_escalated_not_reopened(self) -> None:
        task = _failed_task()
        _add_failed_attempt(task, error="result_error: no terminal ResultMessage")
        _add_failed_attempt(task, error="result_error: no terminal ResultMessage")

        reopened = requeue_transient_failed()

        task.refresh_from_db()
        assert reopened == 0
        assert task.status == Task.Status.FAILED
        assert DeferredQuestion.objects.filter(answered_at__isnull=True).count() == 1

    def test_budget_exhausted_is_escalated_not_reopened(self) -> None:
        cap = max_phase_iterations()
        task = _failed_task()
        # DISTINCT transient errors so no stall — the CAP is what stops it.
        for i in range(cap):
            _add_failed_attempt(task, error=f"result_error: attempt {'x' * (i + 1)} died")

        reopened = requeue_transient_failed()

        task.refresh_from_db()
        assert reopened == 0
        assert task.status == Task.Status.FAILED
        assert DeferredQuestion.objects.filter(answered_at__isnull=True).count() == 1

    def test_tick_recovery_reopens_transient_failed(self) -> None:
        # The sweep is wired into the loop tick's recovery step, not just callable.
        task = _failed_task()
        _add_failed_attempt(task, error="outage_death: connection refused")

        _reap_stale_task_claims()

        task.refresh_from_db()
        assert task.status == Task.Status.PENDING

    def test_bounded_never_retries_endlessly(self) -> None:
        cap = max_phase_iterations()
        task = _failed_task()
        distinct_errors = [
            "outage_death: alpha",
            "result_error: beta gone",
            "provision_failed: gamma missing",
            "landing_unverified: delta uncommitted",
            "outage_death: epsilon",
            "result_error: zeta gone",
            "provision_failed: eta missing",
        ]
        reopens = 0
        for error in distinct_errors:
            _add_failed_attempt(task, error=error)
            reopens += requeue_transient_failed()
            task.refresh_from_db()

        # Reopens are bounded by the per-phase cap — never once per tick forever.
        assert reopens == cap - 1
        assert task.status == Task.Status.FAILED

        # Escalated exactly once; re-running the sweep many more times never
        # reopens again and never spams another escalation.
        questions_after_exhaustion = DeferredQuestion.objects.filter(answered_at__isnull=True).count()
        assert questions_after_exhaustion == 1
        for _ in range(10):
            assert requeue_transient_failed() == 0
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert DeferredQuestion.objects.filter(answered_at__isnull=True).count() == 1
