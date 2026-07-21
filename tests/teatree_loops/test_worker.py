"""teatree.loops.worker — the singleton executor pool + supervisor (#1796).

Pure supervision/lifecycle logic with injected collaborators — no real threads, DB,
or clock. Verifies startup reconciliation, the executor split (2 ``loops`` + a
host-scaled ``default`` pool floored at 2), and that a kill-switch flip-off OR a
stop signal tears the pool down.
"""

import contextlib
import datetime as dt
import os
import sqlite3

import pytest
from django.db import connection
from django.test import TestCase, override_settings
from django.utils import timezone
from django_tasks.base import TaskResultStatus
from django_tasks_db.models import DBTaskResult

from teatree.core.tasks import refresh_followup_snapshot
from teatree.loops import timer_chains
from teatree.loops import worker as worker_mod
from teatree.loops.worker import (
    DEFAULT_QUEUE_FLOOR,
    LOOPS_EXECUTOR_FLOOR,
    LoopWorker,
    LoopWorkerExecutorCrashError,
    WorkerSeams,
    build_executor_queues,
    default_queue_executor_count,
    loops_executor_count,
)
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
    def __init__(self, *, alive: bool = True) -> None:
        self.joined = False
        self._alive = alive

    def is_alive(self) -> bool:
        return self._alive

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
    assert order.count("spawn") == len(build_executor_queues())


def test_both_pools_scale_with_host_cores(monkeypatch: pytest.MonkeyPatch) -> None:
    # 4 loops + 4 default executors on a host whose shared PR-01 ceiling is 4 (an 8-core
    # box). Scaling the loops pool too means two slow ticks no longer stall every OTHER loop.
    monkeypatch.setattr(worker_mod, "default_provision_concurrency", lambda: 4)
    assert loops_executor_count() == 4
    assert default_queue_executor_count() == 4
    queues = build_executor_queues()
    assert queues.count("loops") == 4
    assert queues.count("default") == 4


def test_both_pools_floored_at_two_on_a_small_host(monkeypatch: pytest.MonkeyPatch) -> None:
    # A 1-2 core box floors both pools at the prior hardcoded minimum, never below.
    monkeypatch.setattr(worker_mod, "default_provision_concurrency", lambda: 1)
    assert loops_executor_count() == LOOPS_EXECUTOR_FLOOR == 2
    assert default_queue_executor_count() == DEFAULT_QUEUE_FLOOR == 2


def test_spawns_host_scaled_loops_and_default_executors(monkeypatch: pytest.MonkeyPatch) -> None:
    # Patch BEFORE _make_worker so the WorkerSeams default_factory reads the host size.
    monkeypatch.setattr(worker_mod, "default_provision_concurrency", lambda: 3)
    worker, built, _ = _make_worker(enabled=lambda: False, sleep=lambda _s: None)
    worker.run()
    queues = [executor.queue for executor in built]
    assert queues.count("loops") == 3
    assert queues.count("default") == 3


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


def _liveness_seams(*, enabled, spawn, make_executor, **overrides) -> WorkerSeams:
    return WorkerSeams(
        enabled=enabled,
        reconcile=lambda: None,
        seed_chains=lambda: None,
        expire=lambda: None,
        make_executor=make_executor,
        spawn=spawn,
        kill_ticks=lambda: None,
        sleep=lambda _s: None,
        poll_seconds=0.0,
        executor_queues=("loops",),
        **overrides,
    )


def test_dead_executor_thread_is_respawned() -> None:
    # An executor thread a swallowed DB error silently killed is detected via is_alive
    # and respawned, so its pinned queue keeps draining instead of freezing the box.
    built: list[_FakeExecutor] = []
    alive_flags = iter([False])  # the FIRST handle is dead at the first poll; respawns are alive

    def make_executor(queue: str, worker_id: str) -> _FakeExecutor:
        executor = _FakeExecutor(queue, worker_id)
        built.append(executor)
        return executor

    def spawn(_executor: _FakeExecutor) -> _FakeHandle:
        return _FakeHandle(alive=next(alive_flags, True))

    states = iter([True, False])  # one supervisory poll, then stop
    seams = _liveness_seams(enabled=lambda: next(states, False), spawn=spawn, make_executor=make_executor)
    LoopWorker(seams).run()

    assert len(built) == 2  # the original dead executor + one respawn
    assert built[1].queue == "loops"


def test_repeated_executor_death_exits_the_worker_non_zero() -> None:
    # A crash-looping executor is a real fault — after the respawn budget is spent the
    # worker raises (exits non-zero) so the OS/container restarts it, never masks it.
    seams = _liveness_seams(
        enabled=lambda: True,  # never flips off — only the crash path terminates run()
        spawn=lambda _e: _FakeHandle(alive=False),  # every executor is dead
        make_executor=_FakeExecutor,
        max_respawns=2,
    )
    with pytest.raises(LoopWorkerExecutorCrashError):
        LoopWorker(seams).run()


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


class TestExecutorThreadConnectionHygiene(TestCase):
    """A spawned executor thread must release its raw DB handle on exit.

    ``connections.close_all()`` — what this thread used to call — is a documented
    no-op for an in-memory sqlite database, so it left the handle stranded for a
    later GC to finalize as a ``ResourceWarning``. Under ``filterwarnings = error``
    that fails an unrelated test in the same xdist worker.
    """

    def test_spawned_thread_closes_its_raw_db_handle(self) -> None:
        raw_connections: list[sqlite3.Connection] = []

        class _OrmTouchingExecutor:
            def run(self) -> None:
                connection.ensure_connection()
                raw_connections.append(connection.connection)

            def stop(self) -> None:
                return None

        thread = worker_mod._spawn_executor_thread(_OrmTouchingExecutor())
        thread.join(timeout=10)

        assert raw_connections, "the executor never opened a connection"
        with pytest.raises(sqlite3.ProgrammingError):
            raw_connections[0].execute("SELECT 1")
