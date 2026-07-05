"""teatree.loops.worker — the singleton executor pool + supervisor (#1796).

Pure supervision/lifecycle logic with injected collaborators — no real threads, DB,
or clock. Verifies startup reconciliation, the pinned executor split (2 ``loops`` /
2 ``default``), and that a kill-switch flip-off OR a stop signal tears the pool down.
"""

import contextlib
import datetime as dt
import os

import pytest
from django.test import override_settings
from django.utils import timezone
from django_tasks.base import TaskResultStatus
from django_tasks_db.models import DBTaskResult

from teatree.core.tasks import refresh_followup_snapshot
from teatree.loops import timer_chains
from teatree.loops.worker import EXECUTOR_QUEUES, LoopWorker, WorkerSeams
from teatree.utils.run import spawn_session_leader
from teatree.utils.singleton import pid_alive


class _FakeExecutor:
    def __init__(self, queue: str, worker_id: str) -> None:
        self.queue = queue
        self.worker_id = worker_id
        self.running = True

    def run(self) -> None:  # never actually invoked — spawn is stubbed
        pass


class _FakeHandle:
    def __init__(self) -> None:
        self.joined = False

    def join(self, timeout: float | None = None) -> None:
        self.joined = True


def _make_worker(*, enabled, sleep, **seam_overrides):
    built: list[_FakeExecutor] = []
    handles: list[_FakeHandle] = []

    def default_make_executor(queue: str, worker_id: str) -> _FakeExecutor:
        executor = _FakeExecutor(queue, worker_id)
        built.append(executor)
        return executor

    def spawn(_executor: _FakeExecutor) -> _FakeHandle:
        handle = _FakeHandle()
        handles.append(handle)
        return handle

    seams = WorkerSeams(
        enabled=enabled,
        reconcile=seam_overrides.get("reconcile") or (lambda: None),
        seed_chains=seam_overrides.get("seed_chains") or (lambda: None),
        expire=seam_overrides.get("expire") or (lambda: None),
        make_executor=seam_overrides.get("make_executor") or default_make_executor,
        spawn=spawn,
        sleep=sleep,
        poll_seconds=0.0,
    )
    return LoopWorker(seams), built, handles


def test_reconciles_seeds_and_expires_before_starting_executors() -> None:
    order: list[str] = []
    worker, _built, _ = _make_worker(
        enabled=lambda: False,  # exit immediately after startup
        sleep=lambda _s: None,
        reconcile=lambda: order.append("reconcile"),
        seed_chains=lambda: order.append("seed"),
        expire=lambda: order.append("expire"),
        make_executor=lambda q, wid: order.append("spawn") or _FakeExecutor(q, wid),
    )
    worker.run()
    # Expiry MUST run before any executor spawns, else a stale default-queue backlog
    # blind-fires the instant the worker starts (the default-ON flip's load-jam class).
    assert order[:3] == ["reconcile", "seed", "expire"]
    assert order[3] == "spawn"
    assert order.count("spawn") == len(EXECUTOR_QUEUES)


def test_pins_two_loops_and_two_default_executors() -> None:
    worker, built, _ = _make_worker(enabled=lambda: False, sleep=lambda _s: None)
    worker.run()
    queues = [executor.queue for executor in built]
    assert queues.count("loops") == 2
    assert queues.count("default") == 2


def test_kill_switch_flip_off_stops_and_joins_all_executors() -> None:
    states = iter([True, False])  # enabled for one poll, then flipped off
    worker, built, handles = _make_worker(enabled=lambda: next(states, False), sleep=lambda _s: None)
    worker.run()
    assert all(not executor.running for executor in built)
    assert all(handle.joined for handle in handles)


def test_stop_signal_tears_the_pool_down() -> None:
    worker, built, handles = None, None, None

    def sleep(_s: float) -> None:
        worker.request_stop()  # simulate SIGTERM arriving during a supervisor sleep

    worker, built, handles = _make_worker(enabled=lambda: True, sleep=sleep)
    worker.run()
    assert all(not executor.running for executor in built)
    assert all(handle.joined for handle in handles)


def test_shutdown_kills_in_flight_tick_process_groups() -> None:
    # A kill-switch flip / SIGTERM mid-tick tears down the executor thread that owned
    # the deadline, orphaning the tick subprocess with no deadline owner. The worker's
    # shutdown must SIGKILL any in-flight tick process group after the join timeout.
    timer_chains._LIVE_TICK_PGIDS.clear()  # process-global registry — isolate from other tests
    proc = spawn_session_leader(["sleep", "30"])  # stands in for an in-flight tick
    pgid = os.getpgid(proc.pid)
    timer_chains._register_tick_pgid(pgid)
    try:
        worker, _, _ = _make_worker(enabled=lambda: False, sleep=lambda _s: None)  # shut down at once
        worker.run()
        with contextlib.suppress(timer_chains.TimeoutExpired):
            proc.wait(timeout=5)
        assert not pid_alive(proc.pid)  # the orphaned group was killed, not left running
    finally:
        timer_chains._unregister_tick_pgid(pgid)
        timer_chains._killpg(pgid)


_DB_BACKEND = {
    "TASKS": {
        "default": {
            "BACKEND": "django_tasks_db.backend.DatabaseBackend",
            "QUEUES": ["default", "loops"],
        }
    }
}


# ast-grep-ignore: ac-django-no-pytest-django-db
@pytest.mark.django_db(transaction=True)
class TestStartupExpiryBeforeSpawn:
    """The worker retires the stale `default`-queue backlog BEFORE spawning executors (PR-28).

    Fable-found bug #2: `db_worker` executors run every READY `default`-queue job
    unconditionally, so a box that queued days-old provision/ship jobs while no worker
    ran would blind-fire them the instant the default-ON worker spawns. `LoopWorker.run`
    runs the real `expire_stale_default_jobs` seam first — so the executors never see a
    stale job.
    """

    def test_stale_default_job_is_failed_before_any_executor_spawns(self) -> None:
        with override_settings(**_DB_BACKEND):
            refresh_followup_snapshot.enqueue()
            DBTaskResult.objects.update(enqueued_at=timezone.now() - dt.timedelta(hours=50))
            job_id = DBTaskResult.objects.get().id

            statuses_at_spawn: list[str] = []

            def make_executor(queue: str, worker_id: str) -> _FakeExecutor:
                # Capture the stale job's status at the moment each executor is built —
                # it must already be FAILED (the expiry ran first), never READY.
                statuses_at_spawn.append(DBTaskResult.objects.get(id=job_id).status)
                return _FakeExecutor(queue, worker_id)

            # Build WorkerSeams directly so `expire` keeps its REAL default
            # (expire_stale_default_jobs) — `_make_worker` stubs it to a no-op.
            seams = WorkerSeams(
                enabled=lambda: False,  # exit right after startup
                reconcile=lambda: None,
                seed_chains=lambda: None,
                make_executor=make_executor,
                spawn=lambda _e: _FakeHandle(),
                sleep=lambda _s: None,
                poll_seconds=0.0,
            )
            LoopWorker(seams).run()

        assert DBTaskResult.objects.get(id=job_id).status == TaskResultStatus.FAILED
        assert statuses_at_spawn  # executors were built
        assert all(status == TaskResultStatus.FAILED for status in statuses_at_spawn)
