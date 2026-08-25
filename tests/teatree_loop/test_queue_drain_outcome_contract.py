"""A queued job whose callable RETURNS failure is recorded FAILED (#4528).

The drain, the vendored ``db_worker`` and ``ImmediateBackend`` all turn a raised
exception into a FAILED row, so these drive the real ``DatabaseBackend`` claim/
run/finish path and assert the returned ``ok=False`` reaches it as a failure.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import override_settings
from django_tasks.base import TaskResultStatus
from django_tasks_db.models import DBTaskResult

from teatree.core.models import Session, Ticket, Worktree
from teatree.loop.queue_drain import drain_ready_batch

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

DB_BACKEND = {
    "TASKS": {
        "default": {
            "BACKEND": "django_tasks_db.backend.DatabaseBackend",
            "QUEUES": ["default", "loops"],
        }
    }
}

_NO_HOST = "teatree.core.runners.ship.code_host_for_repo_from_overlay"


@pytest.fixture(autouse=True)
def _db_task_backend() -> object:
    with override_settings(**DB_BACKEND):
        yield


@pytest.fixture(autouse=True)
def _isolate_singleton_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the worker-singleton probe at an empty per-test dir, as the sibling lane does."""
    from teatree.utils import singleton as singleton_mod  # noqa: PLC0415 — test-local: patch the module attr

    monkeypatch.setattr(singleton_mod, "DATA_DIR", tmp_path / "singletons")
    (tmp_path / "singletons").mkdir()


def _shipping_ticket() -> Ticket:
    ticket = Ticket.objects.create(overlay="test", state=Ticket.State.SHIPPED)
    Worktree.objects.create(
        ticket=ticket,
        overlay="test",
        repo_path="/tmp/backend",
        branch="feature-branch",
        extra={"worktree_path": "/tmp/backend"},
    )
    return ticket


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db(transaction=True)
class TestQueuedShipFailureIsRecordedFailed:
    def test_returned_ship_failure_lands_as_a_failed_job(self) -> None:
        from teatree.core.tasks import execute_ship  # noqa: PLC0415 — deferred: needs the app registry

        ticket = _shipping_ticket()
        execute_ship.enqueue(int(ticket.pk))

        with patch(_NO_HOST, return_value=None):
            assert drain_ready_batch(max_jobs=5) == 1

        job = DBTaskResult.objects.get()
        assert job.status == TaskResultStatus.FAILED
        assert "no code host configured" in job.traceback

    def test_the_ticket_does_not_advance_past_shipped(self) -> None:
        from teatree.core.tasks import execute_ship  # noqa: PLC0415 — deferred: needs the app registry

        ticket = _shipping_ticket()
        execute_ship.enqueue(int(ticket.pk))

        with patch(_NO_HOST, return_value=None):
            drain_ready_batch(max_jobs=5)

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SHIPPED

    def test_a_successful_ship_still_lands_as_a_successful_job(self) -> None:
        """The guard must not turn every ship into a failure."""
        from teatree.core.runners import RunnerResult  # noqa: PLC0415 — deferred: needs the app registry
        from teatree.core.tasks import execute_ship  # noqa: PLC0415 — deferred: needs the app registry

        ticket = _shipping_ticket()
        execute_ship.enqueue(int(ticket.pk))

        with patch(
            "teatree.core.runners.ShipExecutor.run",
            return_value=RunnerResult(ok=True, detail="PR opened"),
        ):
            drain_ready_batch(max_jobs=5)

        assert DBTaskResult.objects.get().status == TaskResultStatus.SUCCESSFUL
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.IN_REVIEW

    def test_a_redelivered_job_whose_ticket_moved_on_is_a_successful_no_op(self) -> None:
        """``skipped`` is at-least-once delivery working, never a failure."""
        from teatree.core.tasks import execute_ship  # noqa: PLC0415 — deferred: needs the app registry

        ticket = Ticket.objects.create(overlay="test", state=Ticket.State.IN_REVIEW)
        execute_ship.enqueue(int(ticket.pk))

        drain_ready_batch(max_jobs=5)

        assert DBTaskResult.objects.get().status == TaskResultStatus.SUCCESSFUL


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db(transaction=True)
class TestTheVendoredWorkerBoundaryIsCoveredToo:
    """``loops.worker._build_executor`` runs this exact class, unpatched (#4528)."""

    def _run_the_queued_job(self) -> DBTaskResult:
        from django_tasks_db.management.commands.db_worker import Worker  # noqa: PLC0415 — heavy/optional dep

        worker = Worker(
            queue_names=["default"],
            interval=0,
            batch=True,
            backend_name="default",
            startup_delay=False,
            max_tasks=1,
            worker_id="test-worker",
        )
        worker.run_task(DBTaskResult.objects.get())
        return DBTaskResult.objects.get()

    def test_a_returned_ship_failure_is_failed_by_the_vendored_worker(self) -> None:
        from teatree.core.tasks import execute_ship  # noqa: PLC0415 — deferred: needs the app registry

        execute_ship.enqueue(int(_shipping_ticket().pk))

        with patch(_NO_HOST, return_value=None):
            job = self._run_the_queued_job()

        assert job.status == TaskResultStatus.FAILED
        assert "no code host configured" in job.traceback


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db(transaction=True)
class TestOtherCallablesConformToTheirDeclaredOutcome:
    """``execute_task`` declares EXIT_CODE: a failed attempt is a failed job (#4528)."""

    def _drain_one_task(self, phase_result: dict[str, str]) -> str:
        from teatree.core.models import Task  # noqa: PLC0415 — deferred: needs the app registry
        from teatree.core.tasks import execute_task  # noqa: PLC0415 — deferred: needs the app registry

        # Creating the row auto-enqueues its own ``execute_task`` job; a second explicit
        # enqueue would leave two rows for one Task.
        ticket = Ticket.objects.create(overlay="", state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, overlay="")
        Task.objects.create(ticket=ticket, session=session, phase="coding")

        with patch("teatree.core.tasks.run_deterministic_phase", return_value=phase_result):
            drain_ready_batch(max_jobs=5)

        return str(DBTaskResult.objects.get(task_path=execute_task.module_path).status)

    def test_a_non_zero_agent_exit_lands_as_a_failed_job(self) -> None:
        status = self._drain_one_task({"exit_code": "1", "phase_error": "the phase raised"})

        assert status == TaskResultStatus.FAILED

    def test_a_clean_agent_exit_still_lands_as_a_successful_job(self) -> None:
        status = self._drain_one_task({"exit_code": "0", "attempt_id": "7"})

        assert status == TaskResultStatus.SUCCESSFUL
