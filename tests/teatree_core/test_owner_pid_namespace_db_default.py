"""An INSERT that OMITS ``owner_pid_namespace`` stores ``""``, not NULL (#4379).

The measured production failure was
``IntegrityError: NOT NULL constraint failed: teatree_task.owner_pid_namespace``
raised while RECORDING a completed run, which rolled the whole
``Task.complete()`` transaction back and stored a successful agent run as
``failed``. Both explanations #4379 offered are wrong: no write path passes
``None`` (``current_owner()`` returns ``""`` on an unreadable procfs), and the
model matches migration ``0069`` exactly. What is true is that Django never
persists a Python ``default`` into the column, so ``AddField(CharField(blank=True,
default=""))`` yields a NOT NULL column with NO database default — and a process
whose in-memory model class predates the field OMITS the column entirely.

These drive that exact shape: a row is copied at the SQL level with every column
EXCEPT the one under test, which is what an older model class emits. The claim
gate (#4387) can only protect a process that runs the gate's own code; the
database default protects every writer, including one older than the gate.
"""

from typing import cast

import django.test

from teatree.core.loop_lease_liveness import reader_pid_namespace
from teatree.core.models.loop_lease import LoopLease
from teatree.core.models.task import Task
from tests.factories import TaskFactory
from tests.teatree_core._row_clone import clone_row_omitting

_COLUMN = "owner_pid_namespace"


class TestTaskOwnerPidNamespaceHasADatabaseDefault(django.test.TestCase):
    def test_an_insert_that_omits_the_column_stores_blank(self) -> None:
        task = cast("Task", TaskFactory())

        clone_row_omitting(Task._meta.db_table, _COLUMN, task.pk)

        clone = Task.objects.exclude(pk=task.pk).get()
        assert clone.owner_pid_namespace == ""

    def test_a_normal_claim_still_stores_the_real_namespace(self) -> None:
        """Anti-vacuity: the DB default must not clobber what a live writer supplies."""
        TaskFactory(status=Task.Status.PENDING)

        claimed = Task.objects.claim_next_pending(claimed_by="worker")

        assert claimed is not None
        claimed.refresh_from_db()
        assert claimed.owner_pid_namespace == reader_pid_namespace()


class TestLoopLeaseOwnerPidNamespaceHasADatabaseDefault(django.test.TestCase):
    def test_an_insert_that_omits_the_column_stores_blank(self) -> None:
        lease = LoopLease.objects.create(name="t3-master", owner="worker", owner_pid_namespace="pid:[1]")

        clone_row_omitting(LoopLease._meta.db_table, _COLUMN, lease.pk, replacing={"name": "t3-master-copy"})

        clone = LoopLease.objects.exclude(pk=lease.pk).get()
        assert clone.owner_pid_namespace == ""
