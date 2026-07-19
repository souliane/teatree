"""Repair-loop robustness: per-phase iteration budget + stall detection (#2009).

Two gaps vs the verify→diagnose→patch→re-verify repair-loop design closed here:
a visible per-ticket/per-phase iteration counter with a configurable cap
(``MaxIterationsExceeded``), and an error-fingerprint stall detector
(``IterationStalled``) that escalates to the user via a ``DeferredQuestion``
instead of burning another attempt re-running the identical failure.
"""

import pytest
from django.test import TestCase, override_settings
from django.utils import timezone

from teatree.core.models import Session, Task, TaskAttempt, Ticket
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.repair_loop import (
    IterationStalled,
    MaxIterationsExceeded,
    max_phase_iterations,
    terminal_reason_fingerprint,
)


def _failed_attempt(task: Task, *, error: str) -> TaskAttempt:
    return TaskAttempt.objects.create(
        task=task,
        execution_target=task.execution_target,
        ended_at=timezone.now(),
        exit_code=1,
        error=error,
    )


class TestIterationCounter(TestCase):
    def _phase_task(self, ticket: Ticket, *, phase: str) -> Task:
        session = Session.objects.create(ticket=ticket, agent_id="agent-1")
        return Task.objects.create(ticket=ticket, session=session, phase=phase)

    def test_iteration_increments_per_attempt_of_same_phase(self) -> None:
        ticket = Ticket.objects.create()
        task = self._phase_task(ticket, phase="coding")
        first = _failed_attempt(task, error="boom-1")
        second = _failed_attempt(task, error="boom-2")
        assert first.iteration == 1
        assert second.iteration == 2

    def test_iteration_is_per_phase_not_global(self) -> None:
        ticket = Ticket.objects.create()
        coding = self._phase_task(ticket, phase="coding")
        testing = self._phase_task(ticket, phase="testing")
        _failed_attempt(coding, error="c1")
        first_testing = _failed_attempt(testing, error="t1")
        # A different phase starts its own iteration sequence at 1.
        assert first_testing.iteration == 1

    def test_iteration_counts_across_requeued_tasks_of_same_phase(self) -> None:
        # A re-queue creates a NEW Task row for the same (ticket, phase); the
        # iteration counter must span those rows, not reset per Task.
        ticket = Ticket.objects.create()
        first_task = self._phase_task(ticket, phase="coding")
        _failed_attempt(first_task, error="c1")
        requeued = self._phase_task(ticket, phase="coding")
        requeued_attempt = _failed_attempt(requeued, error="c2")
        assert requeued_attempt.iteration == 2
        assert requeued.phase_iteration_count() == 2

    def test_short_verb_and_gerund_count_as_same_phase(self) -> None:
        ticket = Ticket.objects.create()
        review = self._phase_task(ticket, phase="review")
        reviewing = self._phase_task(ticket, phase="reviewing")
        _failed_attempt(review, error="r1")
        second = _failed_attempt(reviewing, error="r2")
        assert second.iteration == 2


class TestTerminalReasonFingerprint(TestCase):
    def test_identical_reasons_hash_identically(self) -> None:
        assert terminal_reason_fingerprint("the same failure") == terminal_reason_fingerprint("the same failure")

    def test_distinct_reasons_hash_differently(self) -> None:
        assert terminal_reason_fingerprint("failure A") != terminal_reason_fingerprint("failure B")

    def test_transient_noise_does_not_defeat_identity(self) -> None:
        # Timestamps / pids / hex ids / tmp paths are transient noise; two
        # otherwise-identical reasons must fingerprint the same so the stall
        # check sees them as "identical".
        a = "[2026-06-24 04:10:23] worker pid=12345 failed at /tmp/abc123/run: lookup error 0xdeadbeef"
        b = "[2026-06-23 19:02:01] worker pid=98765 failed at /tmp/zzz999/run: lookup error 0xfeedface"
        assert terminal_reason_fingerprint(a) == terminal_reason_fingerprint(b)

    def test_empty_reason_has_no_fingerprint(self) -> None:
        assert terminal_reason_fingerprint("") == ""
        assert terminal_reason_fingerprint("   ") == ""


class TestAttemptStampsFingerprint(TestCase):
    def test_failed_attempt_stamps_fingerprint(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="a")
        task = Task.objects.create(ticket=ticket, session=session, phase="coding")
        attempt = _failed_attempt(task, error="some failure")
        assert attempt.error_fingerprint == terminal_reason_fingerprint("some failure")

    def test_clean_attempt_has_empty_fingerprint(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="a")
        task = Task.objects.create(ticket=ticket, session=session, phase="coding")
        attempt = TaskAttempt.objects.create(
            task=task,
            execution_target=task.execution_target,
            ended_at=timezone.now(),
            exit_code=0,
        )
        assert attempt.error_fingerprint == ""


class TestMaxPhaseIterationsConfig(TestCase):
    def test_default_is_a_sensible_positive_cap(self) -> None:
        assert max_phase_iterations() >= 1

    @override_settings(MAX_PHASE_ITERATIONS=2)
    def test_setting_overrides_default(self) -> None:
        assert max_phase_iterations() == 2


@override_settings(MAX_PHASE_ITERATIONS=3)
class TestCheckRequeueAllowed(TestCase):
    def _phase_task(self, ticket: Ticket, *, phase: str = "coding") -> Task:
        session = Session.objects.create(ticket=ticket, agent_id="agent-1")
        return Task.objects.create(ticket=ticket, session=session, phase=phase)

    def test_under_cap_with_distinct_failures_is_allowed(self) -> None:
        ticket = Ticket.objects.create()
        task = self._phase_task(ticket)
        _failed_attempt(task, error="failure A")
        # One attempt so far, distinct failures only — re-queue is fine.
        task.check_requeue_allowed()  # must not raise

    def test_at_cap_raises_max_iterations_exceeded(self) -> None:
        ticket = Ticket.objects.create()
        task = self._phase_task(ticket)
        _failed_attempt(task, error="A")
        _failed_attempt(task, error="B")
        _failed_attempt(task, error="C")
        with pytest.raises(MaxIterationsExceeded):
            task.check_requeue_allowed()

    def test_distinct_fingerprints_do_not_trip_stall_below_cap(self) -> None:
        ticket = Ticket.objects.create()
        task = self._phase_task(ticket)
        _failed_attempt(task, error="failure A")
        _failed_attempt(task, error="failure B")
        # 2 attempts < cap 3, distinct fingerprints → keep retrying, no raise.
        task.check_requeue_allowed()

    def test_two_identical_fingerprints_raise_iteration_stalled(self) -> None:
        ticket = Ticket.objects.create()
        task = self._phase_task(ticket)
        _failed_attempt(task, error="the identical failure")
        _failed_attempt(task, error="the identical failure")
        with pytest.raises(IterationStalled):
            task.check_requeue_allowed()

    def test_stall_escalates_via_deferred_question(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/1")
        task = self._phase_task(ticket)
        _failed_attempt(task, error="the identical failure")
        _failed_attempt(task, error="the identical failure")
        with pytest.raises(IterationStalled):
            task.check_requeue_allowed()
        pending = DeferredQuestion.pending()
        assert pending.count() == 1
        assert "stall" in pending.first().question.lower()

    def test_stall_escalation_is_internal_audience_not_dmed(self) -> None:
        # Phase 2: a repair-loop stall is the box's own health, not an owner
        # question — it is recorded INTERNAL so the poster never DMs it.
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/9")
        task = self._phase_task(ticket)
        _failed_attempt(task, error="the identical failure")
        _failed_attempt(task, error="the identical failure")
        with pytest.raises(IterationStalled):
            task.check_requeue_allowed()
        row = DeferredQuestion.pending().get()
        assert row.audience == DeferredQuestion.Audience.INTERNAL

    def test_two_consecutive_stalls_record_a_single_deferred_question(self) -> None:
        # F2 dedupe: re-stalling on the same ticket-phase escalates once, not once
        # per tick — the escalate-once dedupe_marker collapses both to one row.
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/7")
        task = self._phase_task(ticket)
        _failed_attempt(task, error="the identical failure")
        _failed_attempt(task, error="the identical failure")
        for _ in range(2):
            with pytest.raises(IterationStalled):
                task.check_requeue_allowed()
        assert DeferredQuestion.pending().count() == 1

    def test_non_consecutive_identical_does_not_stall(self) -> None:
        ticket = Ticket.objects.create()
        task = self._phase_task(ticket)
        _failed_attempt(task, error="same")
        _failed_attempt(task, error="different")
        # last two are (different, same)-ordered → not two-consecutive-identical.
        task.check_requeue_allowed()


@override_settings(MAX_PHASE_ITERATIONS=3)
class TestReclaimEnforcesRepairLoop(TestCase):
    """The re-queue chokepoint (``reclaim_orphaned_claims``) honours the budget."""

    def _orphaned_phase_task(self, ticket: Ticket, *, phase: str = "coding") -> Task:
        session = Session.objects.create(ticket=ticket, agent_id="agent-1")
        past = timezone.now() - timezone.timedelta(seconds=600)
        return Task.objects.create(
            ticket=ticket,
            session=session,
            phase=phase,
            status=Task.Status.CLAIMED,
            claimed_by="worker",
            claimed_at=past,
            lease_expires_at=past,
        )

    def test_distinct_failures_below_cap_are_requeued(self) -> None:
        ticket = Ticket.objects.create()
        task = self._orphaned_phase_task(ticket)
        _failed_attempt(task, error="failure A")
        reclaimed = Task.objects.reclaim_orphaned_claims()
        task.refresh_from_db()
        assert reclaimed == 1
        assert task.status == Task.Status.PENDING

    def test_stalled_phase_is_not_requeued_and_escalates(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/2")
        task = self._orphaned_phase_task(ticket)
        _failed_attempt(task, error="the identical failure")
        _failed_attempt(task, error="the identical failure")
        Task.objects.reclaim_orphaned_claims()
        task.refresh_from_db()
        # NOT returned to PENDING — the stall refused the re-queue.
        assert task.status != Task.Status.PENDING
        assert DeferredQuestion.pending().count() == 1
        # No NEW attempt was created by the refused re-queue.
        assert task.attempts.count() == 2

    def test_over_cap_phase_is_not_requeued(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/3")
        task = self._orphaned_phase_task(ticket)
        _failed_attempt(task, error="A")
        _failed_attempt(task, error="B")
        _failed_attempt(task, error="C")
        Task.objects.reclaim_orphaned_claims()
        task.refresh_from_db()
        assert task.status != Task.Status.PENDING
