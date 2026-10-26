"""A crashed critic is decided by its own dispatch budget, not by the ticket-phase budget."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import timedelta
from threading import Barrier, BrokenBarrierError
from unittest.mock import patch

from django.db import connection, connections
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from teatree.core.models import Session, Task, TaskAttempt, Ticket
from teatree.core.models.auto_review_dispatch import MAX_DISPATCH_ATTEMPTS
from teatree.core.models.critic_dispatch import CriticDispatch
from teatree.core.models.critic_verdict import CriticVerdict
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.repair_loop import max_phase_iterations
from teatree.loop import transient_requeue
from teatree.loop.transient_requeue import HALT_STAMP, requeue_transient_failed
from teatree.loop.transient_requeue_disposal import LIVE_SUCCESSOR_STAMP
from tests._file_backed_db import file_backed_default_database

_HEAD = "a" * 40
_CRASH = "result_error: the critic session died"
_ASSERTION = "AssertionError: expected 3 got 4"


def _crash(task: Task, *, error: str = _CRASH, times: int = 1) -> None:
    for index in range(times):
        TaskAttempt.objects.create(task=task, ended_at=timezone.now(), exit_code=1, error=f"{error} {'x' * index}")
    Task.objects.filter(pk=task.pk).update(status=Task.Status.FAILED)


def _open_questions() -> int:
    return DeferredQuestion.objects.filter(answered_at__isnull=True).count()


class TestACrashedCriticIsDecidedByItsDispatchBudget(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.WORK_STARTED)
        dispatch = CriticDispatch.enqueue(ticket=self.ticket, transition="merge", head_sha=_HEAD, contract="grade")
        assert dispatch is not None
        assert dispatch.task is not None
        self.dispatch = dispatch
        self.task = dispatch.task

    def _crashed_over_the_ticket_phase_budget(self, **kwargs: str) -> None:
        _crash(self.task, times=max_phase_iterations(), **kwargs)

    def _attempts(self) -> int:
        return CriticDispatch.objects.get(pk=self.dispatch.pk).attempts

    def test_a_stale_sweep_cannot_spend_an_attempt_after_the_task_reopened(self) -> None:
        _crash(self.task)
        assert requeue_transient_failed() == 1

        assert CriticDispatch.spend_reopen(self.task) is False
        assert self._attempts() == 2

    def test_a_crash_with_dispatch_budget_left_is_reopened_and_spends_one_attempt(self) -> None:
        self._crashed_over_the_ticket_phase_budget()

        assert requeue_transient_failed() == 1

        self.task.refresh_from_db()
        assert self.task.status == Task.Status.PENDING
        assert self._attempts() == 2
        assert _open_questions() == 0

    def test_a_reopened_critic_is_not_saturated_while_it_runs(self) -> None:
        CriticDispatch.objects.filter(pk=self.dispatch.pk).update(
            attempts=MAX_DISPATCH_ATTEMPTS - 1, deadline=timezone.now() - timedelta(hours=1)
        )
        self._crashed_over_the_ticket_phase_budget()

        assert requeue_transient_failed() == 1

        assert not CriticDispatch.saturated().filter(pk=self.dispatch.pk).exists()

    def test_a_crash_at_the_dispatch_cap_escalates_once(self) -> None:
        CriticDispatch.objects.filter(pk=self.dispatch.pk).update(attempts=MAX_DISPATCH_ATTEMPTS)
        self._crashed_over_the_ticket_phase_budget()

        assert requeue_transient_failed() == 0
        assert requeue_transient_failed() == 0

        self.task.refresh_from_db()
        assert self.task.status == Task.Status.FAILED
        assert HALT_STAMP in self.task.execution_reason
        assert self._attempts() == MAX_DISPATCH_ATTEMPTS
        assert _open_questions() == 1

    def test_the_last_attempt_is_spent_then_the_next_crash_escalates(self) -> None:
        CriticDispatch.objects.filter(pk=self.dispatch.pk).update(attempts=MAX_DISPATCH_ATTEMPTS - 1)
        self._crashed_over_the_ticket_phase_budget()
        assert requeue_transient_failed() == 1
        assert self._attempts() == MAX_DISPATCH_ATTEMPTS

        _crash(self.task, error="result_error: the reopened critic died too")

        assert requeue_transient_failed() == 0
        assert _open_questions() == 1

    def test_a_covering_verdict_parks_the_crash_without_a_question(self) -> None:
        CriticVerdict.objects.create(ticket=self.ticket, transition="merge", head_sha=_HEAD, grader_identity="critic")
        self._crashed_over_the_ticket_phase_budget()

        assert requeue_transient_failed() == 0

        self._assert_parked_silently()

    def test_a_resolved_dispatch_parks_the_crash_without_a_question(self) -> None:
        CriticDispatch.mark_resolved(ticket=self.ticket, transition="merge", head_sha=_HEAD)
        self._crashed_over_the_ticket_phase_budget()

        assert requeue_transient_failed() == 0

        self._assert_parked_silently()

    def test_a_dispatch_re_armed_onto_a_newer_task_parks_the_old_crash_without_a_question(self) -> None:
        _crash(self.task)
        rearmed = CriticDispatch.rearm_unverified(self.task)
        assert rearmed is not None
        assert rearmed.task is not None
        assert rearmed.task.pk != self.task.pk
        Task.objects.filter(pk=rearmed.task.pk).update(status=Task.Status.COMPLETED)
        attempts_after_rearm = self._attempts()
        self._crashed_over_the_ticket_phase_budget()

        assert requeue_transient_failed() == 0

        self._assert_parked_silently(attempts=attempts_after_rearm)

    def test_a_covering_verdict_does_not_excuse_a_deterministic_failure(self) -> None:
        CriticVerdict.objects.create(ticket=self.ticket, transition="merge", head_sha=_HEAD, grader_identity="critic")
        _crash(self.task, error=_ASSERTION)

        assert requeue_transient_failed() == 0

        assert LIVE_SUCCESSOR_STAMP not in Task.objects.get(pk=self.task.pk).execution_reason
        assert _open_questions() == 1

    def _assert_parked_silently(self, *, attempts: int = 1) -> None:
        self.task.refresh_from_db()
        assert self.task.status == Task.Status.FAILED
        assert LIVE_SUCCESSOR_STAMP in self.task.execution_reason
        assert self._attempts() == attempts
        assert _open_questions() == 0


class TestCriticRetryTransaction(TransactionTestCase):
    def test_dispatch_spend_and_task_reopen_share_one_transaction(self) -> None:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.WORK_STARTED)
        dispatch = CriticDispatch.enqueue(ticket=ticket, transition="merge", head_sha=_HEAD, contract="grade")
        assert dispatch is not None
        assert dispatch.task is not None
        _crash(dispatch.task)
        transactions: dict[str, object | None] = {}
        original_spend = CriticDispatch.spend_reopen
        original_reopen = transient_requeue._reopen

        def spend(task: Task) -> bool:
            transactions["spend"] = connection.atomic_blocks[-1] if connection.in_atomic_block else None
            return original_spend(task)

        def reopen(task: Task) -> int:
            transactions["reopen"] = connection.atomic_blocks[-1] if connection.in_atomic_block else None
            return original_reopen(task)

        with (
            patch.object(CriticDispatch, "spend_reopen", side_effect=spend),
            patch("teatree.loop.transient_requeue._reopen", side_effect=reopen),
            patch("teatree.core.managers_task_claim.claim_admission_block_reason", return_value=""),
        ):
            assert requeue_transient_failed() == 1

        assert transactions["spend"] is not None
        assert transactions["spend"] is transactions["reopen"]


class TestTwoWorkerCriticRetry(TransactionTestCase):
    def test_two_workers_spend_one_attempt(self) -> None:
        with file_backed_default_database():
            ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.WORK_STARTED)
            dispatch = CriticDispatch.enqueue(ticket=ticket, transition="merge", head_sha=_HEAD, contract="grade")
            assert dispatch is not None
            assert dispatch.task is not None
            _crash(dispatch.task)

            both_scanned = Barrier(2)
            both_spent = Barrier(2)
            original_scan = transient_requeue._non_terminal_failed_tasks
            original_reopen = transient_requeue._reopen

            def scan() -> list[Task]:
                tasks = original_scan()
                both_scanned.wait(timeout=10)
                return tasks

            def sweep() -> int:
                try:
                    return requeue_transient_failed()
                finally:
                    connections.close_all()

            def reopen(task: Task) -> int:
                with suppress(BrokenBarrierError):
                    both_spent.wait(timeout=0.5)
                return original_reopen(task)

            with (
                patch("teatree.loop.transient_requeue._non_terminal_failed_tasks", side_effect=scan),
                patch("teatree.loop.transient_requeue._reopen", side_effect=reopen),
                patch("teatree.core.managers_task_claim.claim_admission_block_reason", return_value=""),
                ThreadPoolExecutor(max_workers=2) as workers,
            ):
                first = workers.submit(sweep)
                second = workers.submit(sweep)
                assert sorted((first.result(timeout=15), second.result(timeout=15))) == [0, 1]

            dispatch.refresh_from_db()
            dispatch.task.refresh_from_db()
            assert dispatch.attempts == 2
            assert dispatch.task.status == Task.Status.PENDING
            assert _open_questions() == 0


class TestOnlyACriticOwnsADispatchBudget(TestCase):
    def _failed(self, ticket: Ticket, phase: str) -> Task:
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase=phase)
        _crash(task, times=max_phase_iterations())
        return task

    def test_a_critic_with_no_dispatch_keeps_the_ticket_phase_budget(self) -> None:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.WORK_STARTED)
        task = self._failed(ticket, "critic_reviewing")

        assert requeue_transient_failed() == 0

        assert Task.objects.get(pk=task.pk).status == Task.Status.FAILED
        assert _open_questions() == 1

    def test_a_coding_task_keeps_the_ticket_phase_budget_beside_a_newer_critic(self) -> None:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.WORK_STARTED)
        coding = self._failed(ticket, "coding")
        dispatch = CriticDispatch.enqueue(ticket=ticket, transition="merge", head_sha=_HEAD, contract="grade")
        assert dispatch is not None
        assert dispatch.task is not None
        Task.objects.filter(pk=dispatch.task.pk).update(status=Task.Status.COMPLETED)

        assert requeue_transient_failed() == 0

        assert HALT_STAMP in Task.objects.get(pk=coding.pk).execution_reason
        assert _open_questions() == 1
