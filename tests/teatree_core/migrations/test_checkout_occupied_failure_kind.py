"""The ``0087`` forward names the occupied-checkout refusals recorded before the kind existed (#4742).

``failure_kind`` is a stored derivation of the reason text, so adding a kind leaves every
historical row that would now carry it at ``unclassified`` — and the stall filters read the
STORED kind off the attempt. Until they are re-named, two consecutive refusals still
fingerprint-collide and park the ticket, which is the defect that stranded 33 tasks.

Driven through the real migration executor from ``0086``, which is the only run that proves
the deployed shape.
"""

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

_BEFORE = ("core", "0086_anthropictokenusage_token_fingerprint")
_AFTER = ("core", "0087_checkout_occupied_failure_kind")
_UNCLASSIFIED = "unclassified"
_CHECKOUT_OCCUPIED = "checkout_occupied"
_REFUSAL = (
    "Checkout /w/t3-teatree/4719-data-loss/teatree is already occupied by task:3531 "
    "(session s1), held since 2026-09-09T22:28:47+00:00, lease expires 2026-09-09T22:58:47+00:00."
)


@pytest.mark.timeout(240)
class TestOccupiedCheckoutRefusalsAreRenamed(TransactionTestCase):
    def setUp(self) -> None:
        self.addCleanup(self._restore_head)

    @staticmethod
    def _restore_head() -> None:
        connection.close()
        call_command("migrate", "core", "--no-input", verbosity=0)

    def _rewind(self) -> MigrationExecutor:
        executor = MigrationExecutor(connection)
        executor.migrate([_BEFORE])
        return executor

    @staticmethod
    def _models(executor: MigrationExecutor, state: tuple[str, str]):
        apps = executor.loader.project_state(state).apps
        return apps.get_model("core", "Task"), apps.get_model("core", "TaskAttempt")

    def _failed_task(self, executor: MigrationExecutor, *, kind: str, reason: str):
        # ONE rendered registry for every row: each ``project_state`` call builds fresh model
        # classes, so mixing two of them fails the FK's isinstance check.
        apps = executor.loader.project_state(_BEFORE).apps
        ticket = apps.get_model("core", "Ticket").objects.create(role="author", state="started")
        session = apps.get_model("core", "Session").objects.create(ticket=ticket, agent_id="testing")
        task_model = apps.get_model("core", "Task")
        attempt_model = apps.get_model("core", "TaskAttempt")
        task = task_model.objects.create(
            ticket=ticket,
            session=session,
            phase="testing",
            status="failed",
            failure_reason=reason,
            failure_kind=kind,
        )
        attempt = attempt_model.objects.create(
            task=task, ended_at=timezone.now(), exit_code=1, error=reason, failure_kind=kind
        )
        return task, attempt

    def _forward(self, executor: MigrationExecutor) -> None:
        executor.loader.build_graph()
        executor.migrate([_AFTER])

    def test_an_unclassified_refusal_is_renamed_on_the_task_and_its_attempt(self) -> None:
        executor = self._rewind()
        task, attempt = self._failed_task(executor, kind=_UNCLASSIFIED, reason=_REFUSAL)

        self._forward(executor)

        task_model, attempt_model = self._models(executor, _AFTER)
        assert task_model.objects.get(pk=task.pk).failure_kind == _CHECKOUT_OCCUPIED
        assert attempt_model.objects.get(pk=attempt.pk).failure_kind == _CHECKOUT_OCCUPIED

    def test_an_unrelated_unclassified_failure_is_left_alone(self) -> None:
        executor = self._rewind()
        task, attempt = self._failed_task(executor, kind=_UNCLASSIFIED, reason="AssertionError: expected 3 got 4")

        self._forward(executor)

        task_model, attempt_model = self._models(executor, _AFTER)
        assert task_model.objects.get(pk=task.pk).failure_kind == _UNCLASSIFIED
        assert attempt_model.objects.get(pk=attempt.pk).failure_kind == _UNCLASSIFIED

    def test_a_row_already_classified_is_never_re_derived(self) -> None:
        """Scoped to ``unclassified`` — the vocabulary had no name for those, and only those."""
        executor = self._rewind()
        task, attempt = self._failed_task(executor, kind="lease_lost", reason=f"stuck_loop: lease lost: {_REFUSAL}")

        self._forward(executor)

        task_model, attempt_model = self._models(executor, _AFTER)
        assert task_model.objects.get(pk=task.pk).failure_kind == "lease_lost"
        assert attempt_model.objects.get(pk=attempt.pk).failure_kind == "lease_lost"
