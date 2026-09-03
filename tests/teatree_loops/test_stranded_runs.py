""":mod:`teatree.loops.stranded_runs` — the reaper for every task path the others skip.

Three reapers touch ``DBTaskResult`` and none of them can free a RUNNING row of an
arbitrary task path: ``ensure_loop_timers`` repairs only ``loop_timer`` rows of an
ADMITTED loop, ``reap_stuck_runs`` filters on ``execute_task``, ``expire_stale_jobs``
retires only READY rows, and ``prune_finished_task_results`` skips READY and RUNNING
alike. Measured on the running box: 33 RUNNING rows surviving indefinitely, the oldest
26 days, across seven task paths — two of which (``drain_headless_chain``,
``execute_headless_task``) no longer exist in the code at all.

The ceiling is per-QUEUE because that is what the row itself carries and what actually
bounds the body: a ``loops`` chain is capped by the deadlined subprocesses it drives, a
``default`` FSM job by the stranded grace two other readers already judge it against.

Age alone never retires a row: the carrier stamped into ``worker_ids`` at claim time must
also be proved gone, so a live-but-slow run is kept and named rather than failed under a
running executor.
"""

import datetime as dt
import os
import tempfile
from pathlib import Path
from unittest import mock

import django.test
from django.utils import timezone
from django_tasks.base import TaskResultStatus
from django_tasks_db.models import DBTaskResult

from teatree.core.tasks import STRANDED_JOB_GRACE_SECONDS, execute_task
from teatree.loops import stranded_runs
from teatree.loops.timer_chains import LOOPS_QUEUE

_DB_TASKS = {"default": {"BACKEND": "django_tasks_db.DatabaseBackend", "QUEUES": ["default", "loops"]}}

#: A path that exists in no module — the shape half these zombies actually have, left
#: behind by a rename. A per-path deadline table would go stale exactly as this did.
_RENAMED_AWAY = "teatree.loops.timer_reconciler.drain_headless_chain"
_DRIVER_PATH = "teatree.loops.off_live_tick_driver.drive_off_live_tick_loops"

#: A worker pid that is not the recorded singleton holder — a replaced worker, so gone
#: whatever the OS has since done with that integer.
_REPLACED_WORKER_PID = 424242


@django.test.override_settings(USE_TZ=True, TASKS=_DB_TASKS)
class TestReapStrandedRuns(django.test.TestCase):
    def setUp(self) -> None:
        DBTaskResult.objects.all().delete()
        self._holds_singleton(os.getpid())

    def _holds_singleton(self, pid: int) -> None:
        """Record *pid* as the live worker-singleton holder, off the real data dir."""
        pid_file = Path(self.enterContext(tempfile.TemporaryDirectory())) / "worker.pid"
        pid_file.write_text(f"{pid}\n", encoding="utf-8")
        self.enterContext(mock.patch("teatree.utils.singleton.default_pid_path", return_value=pid_file))

    def _running(
        self,
        *,
        task_path: str,
        queue_name: str,
        age_seconds: float,
        worker_id: str = f"worker-{_REPLACED_WORKER_PID}-0-loops",
    ) -> DBTaskResult:
        result = execute_task.enqueue(1, "coding")
        DBTaskResult.objects.filter(id=result.id).update(
            task_path=task_path,
            queue_name=queue_name,
            status=TaskResultStatus.RUNNING,
            started_at=timezone.now() - dt.timedelta(seconds=age_seconds),
            worker_ids=[worker_id] if worker_id else [],
        )
        return DBTaskResult.objects.get(id=result.id)

    def test_reaps_a_loops_chain_row_past_its_ceiling(self) -> None:
        ceiling = stranded_runs.stranded_after_seconds(LOOPS_QUEUE)
        row = self._running(task_path=_DRIVER_PATH, queue_name=LOOPS_QUEUE, age_seconds=ceiling + 60)

        counts = stranded_runs.reap_stranded_runs()

        assert counts == {"failed": 1}
        row.refresh_from_db()
        assert row.status == TaskResultStatus.FAILED

    def test_reaps_a_row_whose_task_path_no_longer_exists(self) -> None:
        # The renamed-away path: unreachable by every per-path reaper, and the exact
        # shape a hand-maintained path -> deadline table would silently stop covering.
        ceiling = stranded_runs.stranded_after_seconds(LOOPS_QUEUE)
        row = self._running(task_path=_RENAMED_AWAY, queue_name=LOOPS_QUEUE, age_seconds=ceiling + 60)

        counts = stranded_runs.reap_stranded_runs()

        assert counts == {"failed": 1}
        row.refresh_from_db()
        assert row.status == TaskResultStatus.FAILED

    def test_leaves_a_loops_chain_row_inside_its_ceiling(self) -> None:
        ceiling = stranded_runs.stranded_after_seconds(LOOPS_QUEUE)
        row = self._running(task_path=_DRIVER_PATH, queue_name=LOOPS_QUEUE, age_seconds=ceiling - 60)

        counts = stranded_runs.reap_stranded_runs()

        assert counts == {"failed": 0}
        row.refresh_from_db()
        assert row.status == TaskResultStatus.RUNNING

    def test_a_default_queue_job_is_judged_against_the_stranded_grace(self) -> None:
        # The bound TeardownDispatch.outstanding_for and the doctor's stranded probe
        # ALREADY read a RUNNING default-queue row against — the reaper makes the table
        # agree with them rather than inventing a second policy.
        assert stranded_runs.stranded_after_seconds("default") == STRANDED_JOB_GRACE_SECONDS

    def test_a_loops_chain_gets_a_longer_ceiling_than_a_default_job(self) -> None:
        # A loop_timer and a drain chain do not have the same healthy runtime as an FSM
        # job: the loops queue drives deadlined subprocesses whose budget dwarfs the grace.
        assert stranded_runs.stranded_after_seconds(LOOPS_QUEUE) > stranded_runs.stranded_after_seconds("default")

    def test_reaps_a_stranded_teardown_on_the_default_queue(self) -> None:
        row = self._running(
            task_path="teatree.core.tasks.execute_teardown",
            queue_name="default",
            age_seconds=STRANDED_JOB_GRACE_SECONDS + 60,
        )

        counts = stranded_runs.reap_stranded_runs()

        assert counts == {"failed": 1}
        row.refresh_from_db()
        assert row.status == TaskResultStatus.FAILED

    def test_never_touches_execute_task(self) -> None:
        # reap_stuck_runs owns it with a process-aware predicate: a stalled-but-ALIVE
        # run must be left entirely alone, or the duplicate wins the claim CAS and two
        # agents land on one worktree (#4164). A flat age ceiling cannot tell those apart.
        row = self._running(
            task_path=execute_task.module_path,
            queue_name="default",
            age_seconds=STRANDED_JOB_GRACE_SECONDS * 100,
        )

        counts = stranded_runs.reap_stranded_runs()

        assert counts == {"failed": 0}
        row.refresh_from_db()
        assert row.status == TaskResultStatus.RUNNING

    def test_never_re_enqueues(self) -> None:
        # Marking FAILED is bookkeeping and cannot kill anything; enqueueing a successor
        # WOULD create a second executor, which is the precise reapers' call, not this one's.
        ceiling = stranded_runs.stranded_after_seconds(LOOPS_QUEUE)
        self._running(task_path=_DRIVER_PATH, queue_name=LOOPS_QUEUE, age_seconds=ceiling + 60)

        stranded_runs.reap_stranded_runs()

        assert DBTaskResult.objects.filter(status=TaskResultStatus.READY).count() == 0

    def test_a_row_with_no_started_at_cannot_be_aged_and_is_left_alone(self) -> None:
        row = self._running(task_path=_DRIVER_PATH, queue_name=LOOPS_QUEUE, age_seconds=0)
        DBTaskResult.objects.filter(id=row.id).update(started_at=None)

        counts = stranded_runs.reap_stranded_runs()

        assert counts == {"failed": 0}
        row.refresh_from_db()
        assert row.status == TaskResultStatus.RUNNING

    def test_ready_and_finished_rows_are_untouched(self) -> None:
        ready = execute_task.enqueue(2, "coding")
        DBTaskResult.objects.filter(id=ready.id).update(task_path=_DRIVER_PATH, queue_name=LOOPS_QUEUE)

        counts = stranded_runs.reap_stranded_runs()

        assert counts == {"failed": 0}
        assert DBTaskResult.objects.get(id=ready.id).status == TaskResultStatus.READY

    def test_a_row_the_live_worker_still_carries_is_left_alone(self) -> None:
        # Age is the reaper's only cheap filter and it cannot tell a slow run from a
        # corpse; the carrier stamped at claim time can. This process holds the
        # singleton, so a row it claimed is in flight right now.
        ceiling = stranded_runs.stranded_after_seconds(LOOPS_QUEUE)
        row = self._running(
            task_path=_DRIVER_PATH,
            queue_name=LOOPS_QUEUE,
            age_seconds=ceiling * 10,
            worker_id=f"worker-{os.getpid()}-0-loops",
        )

        counts = stranded_runs.reap_stranded_runs()

        assert counts == {"failed": 0}
        row.refresh_from_db()
        assert row.status == TaskResultStatus.RUNNING

    def test_a_kept_row_is_named_in_the_log(self) -> None:
        ceiling = stranded_runs.stranded_after_seconds(LOOPS_QUEUE)
        row = self._running(
            task_path=_DRIVER_PATH,
            queue_name=LOOPS_QUEUE,
            age_seconds=ceiling * 10,
            worker_id=f"worker-{os.getpid()}-0-loops",
        )

        with self.assertLogs(stranded_runs.logger, level="INFO") as captured:
            stranded_runs.reap_stranded_runs()

        assert any(str(row.id) in line for line in captured.output), captured.output

    def test_a_live_tick_drain_carrier_keeps_its_row(self) -> None:
        # The tick drain runs outside the singleton, so its claim is judged by the pid
        # probe instead — and an alive pid is not proof of death.
        row = self._running(
            task_path="teatree.core.tasks.execute_teardown",
            queue_name="default",
            age_seconds=STRANDED_JOB_GRACE_SECONDS * 10,
            worker_id=f"tickdrain-{os.getpid()}-abc123",
        )

        counts = stranded_runs.reap_stranded_runs()

        assert counts == {"failed": 0}
        row.refresh_from_db()
        assert row.status == TaskResultStatus.RUNNING

    def test_a_row_with_no_recorded_carrier_cannot_be_judged_and_is_kept(self) -> None:
        row = self._running(
            task_path=_DRIVER_PATH,
            queue_name=LOOPS_QUEUE,
            age_seconds=stranded_runs.stranded_after_seconds(LOOPS_QUEUE) * 10,
            worker_id="",
        )

        counts = stranded_runs.reap_stranded_runs()

        assert counts == {"failed": 0}
        row.refresh_from_db()
        assert row.status == TaskResultStatus.RUNNING

    def test_the_recorded_reason_names_the_carrier_it_proved_gone(self) -> None:
        # The reason is the only record the operator gets, so it may state what was
        # established and nothing more.
        ceiling = stranded_runs.stranded_after_seconds(LOOPS_QUEUE)
        row = self._running(task_path=_DRIVER_PATH, queue_name=LOOPS_QUEUE, age_seconds=ceiling + 60)

        stranded_runs.reap_stranded_runs()

        row.refresh_from_db()
        assert f"worker-{_REPLACED_WORKER_PID}-0-loops" in row.traceback
