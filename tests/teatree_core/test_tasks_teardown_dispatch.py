"""The teardown enqueue seam queues once per outstanding job, not once per call (#3879)."""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings

from teatree.core.models import Ticket
from teatree.core.tasks import STRANDED_JOB_GRACE_SECONDS, TeardownDispatch

IMMEDIATE_BACKEND = {
    "TASKS": {
        "default": {
            "BACKEND": "django_tasks.backends.immediate.ImmediateBackend",
        },
    },
}


class TestEnqueueTeardownBacklogDrain(TestCase):
    """The one-shot operational drain enqueues teardown for terminal tickets holding worktrees."""

    def _terminal_ticket_with_worktree(self, state: Ticket.State) -> Ticket:
        from teatree.core.models import Worktree  # noqa: PLC0415 - deferred: local import

        ticket = Ticket.objects.create(overlay="test")
        ticket.state = state
        ticket.save(update_fields=["state"])
        Worktree.objects.create(
            ticket=ticket, overlay="test", repo_path="r", branch="b", extra={"worktree_path": "/tmp/wt"}
        )
        return ticket

    @override_settings(**IMMEDIATE_BACKEND)
    def test_enqueues_only_terminal_tickets_with_worktrees(self) -> None:
        merged = self._terminal_ticket_with_worktree(Ticket.State.MERGED)
        ignored = self._terminal_ticket_with_worktree(Ticket.State.IGNORED)
        # A terminal ticket with NO worktree: nothing to reap.
        Ticket.objects.create(overlay="test", state=Ticket.State.DELIVERED)
        # A non-terminal ticket with a worktree: not eligible.
        non_terminal = self._terminal_ticket_with_worktree(Ticket.State.IN_REVIEW)

        import teatree.core.tasks as tasks_mod  # noqa: PLC0415 - deferred: the module object the seam looks up

        with patch.object(tasks_mod, "execute_teardown") as teardown:
            enqueued = TeardownDispatch.drain_terminal_backlog()

        assert sorted(enqueued) == sorted([merged.pk, ignored.pk])
        assert non_terminal.pk not in enqueued
        assert teardown.enqueue.call_count == 2


DATABASE_BACKEND = {
    "TASKS": {
        "default": {
            "BACKEND": "django_tasks_db.DatabaseBackend",
            "QUEUES": ["default", "loops"],
        },
    },
}


@override_settings(**DATABASE_BACKEND)
class TestTeardownEnqueueIsIdempotentInSideEffects(TestCase):
    """The teardown enqueue seam mints once per outstanding job, not once per call (#3879).

    Every enqueue leaves a durable ``DBTaskResult`` row, and teardown is safe to
    repeat — so a caller that re-ran it at tick cadence converged the STATE while
    minting hundreds of thousands of rows for work that was already queued. Being
    idempotent in the end state is not the same as being idempotent in the side
    effects, and the row count is the side effect.

    Every direction is pinned separately, because the guard is as dangerous when it
    is too eager as when it is absent. READY, or RUNNING with a live worker, is a
    duplicate: the queued job will do exactly this work, so a second row buys
    nothing. FINISHED (SUCCESSFUL or FAILED) is not: the reaper refuses rather than
    raises when it leaves a worktree standing, so a job that already ran must never
    block the next genuine attempt. RUNNING past the stranded grace is not: nothing
    reaps a row whose worker was killed, so an unbounded RUNNING arm would let one
    crash permanently disable teardown, including the operator's escape hatch. And a
    different job for the same ticket is not: the sibling workers all take a single
    int arg, so only ``task_path`` separates them.

    Runs on the production ``DatabaseBackend`` rather than the suite's dummy: a
    backend that queues nothing leaves no outstanding row to deduplicate against,
    so the test would pass without proving anything.
    """

    @staticmethod
    def _terminal_ticket_with_worktree() -> Ticket:
        from teatree.core.models import Worktree  # noqa: PLC0415 - deferred: local import

        ticket = Ticket.objects.create(overlay="test")
        ticket.state = Ticket.State.MERGED
        ticket.save(update_fields=["state"])
        Worktree.objects.create(
            ticket=ticket, overlay="test", repo_path="r", branch="b", extra={"worktree_path": "/tmp/wt"}
        )
        return ticket

    @staticmethod
    def _queued_rows(ticket: Ticket) -> int:
        from django_tasks_db.models import DBTaskResult  # noqa: PLC0415 - deferred: local import

        return DBTaskResult.objects.filter(task_path=TeardownDispatch.TASK_PATH, args_kwargs__args=[ticket.pk]).count()

    @staticmethod
    def _settle(ticket: Ticket, status: str) -> None:
        """Finish the ticket's teardown row, as a real run does: RUNNING then terminal.

        ``started_at`` is stamped recent on purpose. A finished job carries the same
        fresh ``started_at`` a live one does, so a guard that aged the timestamp
        without also reading the status would call a just-SUCCEEDED job "in flight".
        Leaving ``started_at`` unset here would hide exactly that defect.
        """
        from django.utils import timezone  # noqa: PLC0415 - deferred: local import
        from django_tasks_db.models import DBTaskResult  # noqa: PLC0415 - deferred: local import

        DBTaskResult.objects.filter(task_path=TeardownDispatch.TASK_PATH, args_kwargs__args=[ticket.pk]).update(
            status=status, started_at=timezone.now()
        )

    @staticmethod
    def _mark_running(ticket: Ticket, *, age_seconds: int) -> None:
        """Put the ticket's teardown row in RUNNING, started *age_seconds* ago.

        ``DBTaskResult`` stamps ``started_at`` in the same save that sets RUNNING, so
        a RUNNING row without one does not occur in production and the age is what
        distinguishes a live worker from a dead one.
        """
        from django.utils import timezone  # noqa: PLC0415 - deferred: local import
        from django_tasks.base import TaskResultStatus  # noqa: PLC0415 - deferred: local import
        from django_tasks_db.models import DBTaskResult  # noqa: PLC0415 - deferred: local import

        DBTaskResult.objects.filter(task_path=TeardownDispatch.TASK_PATH, args_kwargs__args=[ticket.pk]).update(
            status=TaskResultStatus.RUNNING,
            started_at=timezone.now() - timedelta(seconds=age_seconds),
        )

    def test_repeated_drains_over_one_backlog_mint_one_job_per_ticket(self) -> None:
        ticket = self._terminal_ticket_with_worktree()

        for _ in range(25):
            TeardownDispatch.drain_terminal_backlog()

        assert self._queued_rows(ticket) == 1, (
            f"{self._queued_rows(ticket)} teardown jobs queued for one un-reaped worktree — "
            "the enqueue rate must be bounded by real work, not by how often the drain is run"
        )

    def test_the_drain_reports_only_the_tickets_it_actually_queued(self) -> None:
        ticket = self._terminal_ticket_with_worktree()

        first = TeardownDispatch.drain_terminal_backlog()
        second = TeardownDispatch.drain_terminal_backlog()

        assert first == [ticket.pk]
        assert second == [], "the drain claimed to queue a ticket whose teardown was already outstanding"

    def test_a_running_job_with_a_live_worker_is_a_duplicate(self) -> None:
        ticket = self._terminal_ticket_with_worktree()
        TeardownDispatch.drain_terminal_backlog()
        self._mark_running(ticket, age_seconds=5)

        TeardownDispatch.drain_terminal_backlog()

        assert self._queued_rows(ticket) == 1

    def test_a_running_job_stranded_past_the_grace_no_longer_blocks_teardown(self) -> None:
        # The guard must not be the reason teardown stops happening. A worker killed
        # mid-run (OOM, container restart) leaves its row RUNNING forever: retention
        # never deletes READY/RUNNING, and no reaper covers this task path. Without a
        # time bound that one row permanently disables BOTH the FSM receiver and the
        # operator drain — and the drain is the documented escape hatch for worktrees
        # that never got reaped, so it must survive a crashed worker.
        ticket = self._terminal_ticket_with_worktree()
        TeardownDispatch.drain_terminal_backlog()
        self._mark_running(ticket, age_seconds=STRANDED_JOB_GRACE_SECONDS + 60)

        assert ticket.pk in TeardownDispatch.drain_terminal_backlog(), (
            "a worker killed mid-teardown permanently suppressed every later attempt"
        )
        assert self._queued_rows(ticket) == 2

    def test_a_teardown_row_is_not_confused_with_another_job_for_the_same_ticket(self) -> None:
        # Every sibling worker (execute_ship, execute_provision, ...) has the identical
        # single-int-arg shape, so the args match on their own. Without the task_path
        # predicate an unrelated queued job for this ticket would read as "teardown is
        # already scheduled" and silently suppress the teardown.
        from teatree.core.tasks import execute_ship  # noqa: PLC0415 - deferred: local import

        ticket = self._terminal_ticket_with_worktree()
        execute_ship.enqueue(ticket.pk)

        assert TeardownDispatch.outstanding_for(ticket.pk) is False
        assert ticket.pk in TeardownDispatch.drain_terminal_backlog()
        assert self._queued_rows(ticket) == 1

    def test_a_finished_job_never_blocks_the_next_attempt(self) -> None:
        # The failure direction the guard must not swallow: the reaper KEEPS a
        # worktree holding unsynced work and reports the refusal, so the job
        # finishes SUCCESSFUL with the worktree still standing. The operator's next
        # drain is a genuine second attempt, not a duplicate.
        from django_tasks.base import TaskResultStatus  # noqa: PLC0415 - deferred: local import

        for finished in (TaskResultStatus.SUCCESSFUL, TaskResultStatus.FAILED):
            ticket = self._terminal_ticket_with_worktree()
            TeardownDispatch.drain_terminal_backlog()
            self._settle(ticket, finished)

            assert ticket.pk in TeardownDispatch.drain_terminal_backlog(), finished
            assert self._queued_rows(ticket) == 2, finished

    def test_a_sibling_ticket_is_never_deduped_away(self) -> None:
        mine = self._terminal_ticket_with_worktree()
        theirs = self._terminal_ticket_with_worktree()

        TeardownDispatch.drain_terminal_backlog()
        TeardownDispatch.drain_terminal_backlog()

        assert self._queued_rows(mine) == 1
        assert self._queued_rows(theirs) == 1

    def test_the_operator_drain_does_not_duplicate_the_fsm_enqueue(self) -> None:
        # The FSM's on-commit enqueue and the operator drain are two callers of one
        # seam. A ticket the FSM already queued must not be queued again by a drain
        # that runs before the worker gets to it.
        from teatree.core.models import Worktree  # noqa: PLC0415 - deferred: local import

        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.IN_REVIEW)
        Worktree.objects.create(
            ticket=ticket, overlay="test", repo_path="r", branch="b", extra={"worktree_path": "/tmp/wt"}
        )
        with self.captureOnCommitCallbacks(execute=True):
            ticket.mark_merged()
            ticket.save()

        assert self._queued_rows(ticket) == 1
        assert TeardownDispatch.drain_terminal_backlog() == []
        assert self._queued_rows(ticket) == 1
