"""Tick-driven drain + stale-job expiry for the django-tasks DB queue.

The queue only advances when something drains it; the #786 loop is
tick-driven and session-bound, so the drain rides the tick. These tests use
real ``DBTaskResult`` rows on the ``DatabaseBackend`` (the production backend)
and only fake the worker-singleton probe — the drain, the expiry, and the
django-tasks claim/run/finish path are all real.

Critically, none of these tests start a live ``db_worker``: the drain runs
in-process against the ephemeral test DB, never the canonical queue.
"""

import datetime as dt
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone
from django_tasks.base import TaskResultStatus
from django_tasks_db.models import DBTaskResult

from teatree.core.models import LoopLease
from teatree.core.tasks import refresh_followup_snapshot
from teatree.loop.queue_drain import (
    drain_ready_batch,
    expire_stale_ready_jobs,
    expire_then_drain,
    stale_threshold_hours,
)

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

DB_BACKEND = {"TASKS": {"default": {"BACKEND": "django_tasks_db.backend.DatabaseBackend"}}}


@pytest.fixture(autouse=True)
def _db_task_backend() -> object:
    with override_settings(**DB_BACKEND):
        yield


def _backdate(hours: float) -> None:
    DBTaskResult.objects.update(enqueued_at=timezone.now() - dt.timedelta(hours=hours))


class TestExpireStaleReadyJobs:
    def test_expires_jobs_older_than_threshold_to_failed(self) -> None:
        refresh_followup_snapshot.enqueue()
        _backdate(50)

        retired = expire_stale_ready_jobs(threshold_hours=24)

        assert retired == {"refresh_followup_snapshot": 1}
        job = DBTaskResult.objects.get()
        assert job.status == TaskResultStatus.FAILED

    def test_records_a_reversible_reason_without_deleting(self) -> None:
        refresh_followup_snapshot.enqueue()
        _backdate(50)
        job_id = DBTaskResult.objects.get().id

        expire_stale_ready_jobs(threshold_hours=24)

        job = DBTaskResult.objects.get(id=job_id)
        assert job.status == TaskResultStatus.FAILED
        assert "stale threshold" in job.traceback
        assert job.args_kwargs is not None

    def test_leaves_fresh_jobs_untouched(self) -> None:
        refresh_followup_snapshot.enqueue()
        _backdate(1)

        retired = expire_stale_ready_jobs(threshold_hours=24)

        assert retired == {}
        assert DBTaskResult.objects.get().status == TaskResultStatus.READY

    def test_only_touches_ready_jobs(self) -> None:
        refresh_followup_snapshot.enqueue()
        _backdate(50)
        DBTaskResult.objects.update(status=TaskResultStatus.RUNNING)

        retired = expire_stale_ready_jobs(threshold_hours=24)

        assert retired == {}
        assert DBTaskResult.objects.get().status == TaskResultStatus.RUNNING


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db(transaction=True)
class TestDrainReadyBatch:
    def test_drains_and_runs_a_ready_job(self) -> None:
        refresh_followup_snapshot.enqueue()

        drained = drain_ready_batch(max_jobs=5)

        assert drained == 1
        assert DBTaskResult.objects.get().status == TaskResultStatus.SUCCESSFUL

    def test_empty_queue_idles_cleanly(self) -> None:
        assert drain_ready_batch(max_jobs=5) == 0

    def test_drain_is_bounded_by_max_jobs(self) -> None:
        for _ in range(4):
            refresh_followup_snapshot.enqueue()

        drained = drain_ready_batch(max_jobs=2)

        assert drained == 2
        assert DBTaskResult.objects.filter(status=TaskResultStatus.READY).count() == 2

    def test_stands_down_when_a_db_worker_holds_the_singleton(self) -> None:
        refresh_followup_snapshot.enqueue()

        with patch("teatree.loop.queue_drain.a_worker_is_running", return_value=True):
            drained = drain_ready_batch(max_jobs=5)

        assert drained == 0
        assert DBTaskResult.objects.get().status == TaskResultStatus.READY

    def test_a_failing_job_is_recorded_failed_without_crashing_the_drainer(self) -> None:
        from teatree.core.tasks import execute_retrospect  # noqa: PLC0415

        execute_retrospect.enqueue(999_999_999)

        drained = drain_ready_batch(max_jobs=5)

        assert drained == 1
        assert DBTaskResult.objects.get().status == TaskResultStatus.FAILED

    def test_no_running_worker_means_drain_proceeds(self) -> None:
        from teatree.loop.queue_drain import a_worker_is_running  # noqa: PLC0415

        assert a_worker_is_running() is False


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db(transaction=True)
class TestExpireThenDrain:
    def test_stale_heavy_job_is_expired_before_it_can_run(self) -> None:
        refresh_followup_snapshot.enqueue()
        _backdate(50)

        result = expire_then_drain()

        assert result["retired"] == {"refresh_followup_snapshot": 1}
        assert result["drained"] == 0
        assert DBTaskResult.objects.get().status == TaskResultStatus.FAILED

    def test_fresh_job_survives_expiry_and_drains(self) -> None:
        refresh_followup_snapshot.enqueue()

        result = expire_then_drain()

        assert result["retired"] == {}
        assert result["drained"] == 1
        assert DBTaskResult.objects.get().status == TaskResultStatus.SUCCESSFUL


class TestThresholdConfig:
    def test_default_threshold(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_QUEUE_STALE_HOURS", raising=False)
        assert stale_threshold_hours() == 24

    def test_override_via_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_QUEUE_STALE_HOURS", "48")
        assert stale_threshold_hours() == 48

    def test_blank_or_garbage_degrades_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_QUEUE_STALE_HOURS", "not-a-number")
        assert stale_threshold_hours() == 24

    def test_floor_prevents_instant_expiry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_QUEUE_STALE_HOURS", "0")
        assert stale_threshold_hours() == 1


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db(transaction=True)
class TestDrainQueueCommand:
    """``manage.py loop_drain_queue`` — the dedicated reactive drain ``/loop`` (replaces the piggyback)."""

    def test_command_runs_expire_then_drain_behind_the_lease(self) -> None:
        refresh_followup_snapshot.enqueue()

        call_command("loop_drain_queue")

        assert DBTaskResult.objects.get().status == TaskResultStatus.SUCCESSFUL

    def test_command_skips_when_lease_is_held(self) -> None:
        LoopLease.objects.acquire("loop-drain-queue", owner="other", lease_seconds=300)
        refresh_followup_snapshot.enqueue()

        call_command("loop_drain_queue")

        assert DBTaskResult.objects.get().status == TaskResultStatus.READY


class TestQueueCommand:
    def test_expire_stale_command_retires_old_jobs(self) -> None:
        refresh_followup_snapshot.enqueue()
        _backdate(50)

        call_command("queue", "expire-stale", "--hours", "24")

        assert DBTaskResult.objects.get().status == TaskResultStatus.FAILED

    def test_dry_run_does_not_mutate(self) -> None:
        refresh_followup_snapshot.enqueue()
        _backdate(50)

        call_command("queue", "expire-stale", "--hours", "24", "--dry-run")

        assert DBTaskResult.objects.get().status == TaskResultStatus.READY

    def test_status_command_is_read_only(self) -> None:
        refresh_followup_snapshot.enqueue()

        call_command("queue", "status")

        assert DBTaskResult.objects.get().status == TaskResultStatus.READY


class TestAdmissionPriorityAnnotation:
    """PR-13: the admission-rank annotation ranks new-ticket auto-starts LAST."""

    def _task(self, *, phase: str, parented: bool = False):
        from teatree.core.models import Session, Task, Ticket  # noqa: PLC0415

        url = f"https://x/{phase}/{Ticket.objects.count()}"
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, issue_url=url, overlay="acme")
        session = Session.objects.create(ticket=ticket, agent_id=f"a-{ticket.pk}")
        parent = Task.objects.create(ticket=ticket, session=session, phase="planning") if parented else None
        return Task.objects.create(ticket=ticket, session=session, phase=phase, parent_task=parent)

    def _rank(self, task) -> int:
        from teatree.core.models import Task  # noqa: PLC0415
        from teatree.loop.queue_drain import ADMISSION_RANK_ALIAS, admission_priority_annotations  # noqa: PLC0415

        row = Task.objects.annotate(**admission_priority_annotations()).get(pk=task.pk)
        return getattr(row, ADMISSION_RANK_ALIAS)

    def test_new_ticket_planning_ranks_last(self) -> None:
        assert self._rank(self._task(phase="planning")) == 1

    def test_new_ticket_scoping_ranks_last(self) -> None:
        assert self._rank(self._task(phase="scoping")) == 1

    def test_short_verb_plan_ranks_last(self) -> None:
        # A short-verb ``plan`` row normalizes to the same auto-start band.
        assert self._rank(self._task(phase="plan")) == 1

    def test_downstream_phase_ranks_first(self) -> None:
        assert self._rank(self._task(phase="coding")) == 0

    def test_followup_planning_ranks_first(self) -> None:
        # A planning task WITH a parent is continuing work, not a new-ticket start.
        assert self._rank(self._task(phase="planning", parented=True)) == 0
