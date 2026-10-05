"""The ``core.models``-hosted occupancy primitives (souliane/teatree#4867).

The CAS itself (``acquire``/``release``/``occupancy_holder``) is pinned end-to-end via
its re-export at :mod:`teatree.core.worktree.occupancy`
(``tests/teatree_core/worktree/test_occupancy.py``) — this file covers only the two
pieces whose home is THIS module: :func:`terminal_task_pk` (the holder-string parse a
non-``core.models`` caller cannot import directly, per the module's own docstring) and
:func:`release_task_occupancy` (exercised end-to-end via ``Task.complete``/``Task.fail``
in ``tests/teatree_core/models/test_task.py``; here it is a direct unit test of the
function's own contract).
"""

import os
import tempfile
from pathlib import Path
from unittest import mock

from django.test import TestCase

import teatree.utils.singleton as singleton_mod
from teatree.core.models import Task, Worktree
from teatree.core.models.worktree_occupancy import release_task_occupancy, terminal_task_pk
from teatree.core.worktree.occupancy import acquire, occupancy_holder, task_holder_id
from tests.factories import SessionFactory, TaskFactory, TicketFactory, WorktreeFactory


class TerminalTaskPkTests(TestCase):
    def test_a_completed_tasks_holder_id_resolves_to_its_pk(self) -> None:
        task = TaskFactory(status=Task.Status.COMPLETED)
        assert terminal_task_pk(task_holder_id(task)) == task.pk

    def test_a_failed_tasks_holder_id_resolves_to_its_pk(self) -> None:
        task = TaskFactory(status=Task.Status.FAILED)
        assert terminal_task_pk(task_holder_id(task)) == task.pk

    def test_a_terminal_task_with_a_confirmed_dead_owner_resolves_to_its_pk(self) -> None:
        """#4872: a third-party fail's withheld claim is still safe to self-heal once proven dead."""
        task = TaskFactory(status=Task.Status.FAILED, owner_pid=os.getpid(), owner_pid_namespace="test-namespace")
        with (
            mock.patch("teatree.core.loop_lease_liveness.reader_pid_namespace", return_value="test-namespace"),
            mock.patch.object(singleton_mod, "pid_alive", return_value=False),
        ):
            assert terminal_task_pk(task_holder_id(task)) == task.pk

    def test_a_terminal_task_with_a_live_owner_withholds_the_self_heal(self) -> None:
        """#4872: the self-heal must not widen a third-party fail's deliberately-kept claim."""
        task = TaskFactory(status=Task.Status.FAILED, owner_pid=os.getpid(), owner_pid_namespace="test-namespace")
        with (
            mock.patch("teatree.core.loop_lease_liveness.reader_pid_namespace", return_value="test-namespace"),
            mock.patch.object(singleton_mod, "pid_alive", return_value=True),
        ):
            assert terminal_task_pk(task_holder_id(task)) is None

    def test_a_task_its_live_holder_completed_resolves_to_its_pk(self) -> None:
        ticket = TicketFactory()
        task = Task.objects.create(ticket=ticket, session=SessionFactory(ticket=ticket), phase="coding")
        task.claim(claimed_by="worker-1", claimed_by_session="sess-1")

        task.complete()

        assert Task.objects.get(pk=task.pk).owner_pid is None
        with mock.patch.object(singleton_mod, "pid_alive", return_value=True):
            assert terminal_task_pk(task_holder_id(task)) == task.pk

    def test_a_live_tasks_holder_id_resolves_to_none(self) -> None:
        task = TaskFactory(status=Task.Status.CLAIMED)
        assert terminal_task_pk(task_holder_id(task)) is None

    def test_a_non_task_holder_resolves_to_none(self) -> None:
        assert terminal_task_pk("operator:someone") is None

    def test_a_malformed_task_holder_resolves_to_none(self) -> None:
        assert terminal_task_pk("task:not-a-number") is None

    def test_a_holder_naming_no_such_task_resolves_to_none(self) -> None:
        assert terminal_task_pk("task:999999999") is None


class ReleaseTaskOccupancyTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.ticket = TicketFactory()
        self.checkout = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: self.checkout.exists() and self.checkout.rmdir())
        self.worktree = WorktreeFactory(ticket=self.ticket, extra={"worktree_path": str(self.checkout)})
        self.task = Task.objects.create(
            ticket=self.ticket,
            session=SessionFactory(ticket=self.ticket),
            claimed_by="worker-1",
            claimed_by_session="sess-1",
        )

    def fresh(self) -> Worktree:
        return Worktree.objects.get(pk=self.worktree.pk)

    def test_releases_the_claim_it_holds(self) -> None:
        acquire(self.worktree, holder=task_holder_id(self.task), holder_session="sess-1")

        assert release_task_occupancy(self.task) is True
        assert occupancy_holder(self.fresh()) is None

    def test_a_task_holding_no_claim_is_a_no_op(self) -> None:
        assert release_task_occupancy(self.task) is False

    def test_never_steals_a_rivals_claim(self) -> None:
        acquire(self.worktree, holder="task:999", holder_session="rival-session")

        assert release_task_occupancy(self.task) is False
        held = occupancy_holder(self.fresh())
        assert held is not None
        assert held.holder == "task:999"

    def test_a_ticket_with_no_materialised_checkout_releases_nothing(self) -> None:
        bare_task = Task.objects.create(ticket=TicketFactory(), session=SessionFactory())
        assert release_task_occupancy(bare_task) is False
