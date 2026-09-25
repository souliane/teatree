"""teatree.loops.worker — the singleton executor pool + supervisor (#1796).

Pure supervision/lifecycle logic with injected collaborators — no real threads, DB,
or clock. Verifies startup reconciliation, the executor split (2 ``loops`` + a
host-scaled ``default`` pool floored at 2), that a preset admitting zero loops quiesces
the pool while the PROCESS stays alive, and that a stop signal tears the pool down.
"""

import contextlib
import dataclasses
import datetime as dt
import inspect
import os
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml
from django.db import connection
from django.tasks import TaskResultStatus
from django.test import TestCase, override_settings
from django.utils import timezone
from django_tasks_db.models import DBTaskResult

from teatree.core.admission_governor import AdmissionDecision
from teatree.core.tasks import refresh_followup_snapshot
from teatree.loops import deadlined_tick
from teatree.loops import worker as worker_mod
from teatree.loops.enable_verdict import FleetAdmission
from teatree.loops.worker import (
    DEFAULT_QUEUE_FLOOR,
    LOOPS_EXECUTOR_FLOOR,
    LoopWorker,
    LoopWorkerExecutorCrashError,
    LoopWorkerExecutorStopError,
    WorkerSeams,
    _bounded_executor_queues,
    build_executor_queues,
    default_queue_executor_count,
    loops_executor_count,
)
from teatree.utils.run import spawn_session_leader
from teatree.utils.singleton import pid_alive

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


def _scripted_reader(script: "Sequence[bool]", holder: "list[LoopWorker]") -> "Callable[[], FleetAdmission]":
    """Feed *script* one verdict per poll, then request stop.

    A preset admitting nothing no longer ends ``run`` — the process stays alive polling —
    so a scripted supervision test needs an explicit end, and exhausting the script is it.
    """
    remaining = iter(script)

    def read() -> FleetAdmission:
        admits = next(remaining, None)
        if admits is None:
            holder[0].request_stop()
            return FleetAdmission.NONE
        return FleetAdmission.ADMITS if admits else FleetAdmission.NONE

    return read


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
        self._alive = False


class _StuckHandle(_FakeHandle):
    def join(self, timeout: float | None = None) -> None:
        self.joined = True


def _make_worker(*, script, sleep, **seam_overrides):
    """A worker whose scripted verdicts end by requesting stop — nothing else exits ``run``."""
    built: list[_FakeExecutor] = []
    handles: list[_FakeHandle] = []
    holder: list[LoopWorker] = []

    def default_make_executor(queue: str, worker_id: str) -> _FakeExecutor:
        executor = _FakeExecutor(queue, worker_id)
        built.append(executor)
        return executor

    def spawn(_executor: _FakeExecutor) -> _FakeHandle:
        handle = _FakeHandle()
        handles.append(handle)
        return handle

    seams = WorkerSeams(
        read_admission=_scripted_reader(script, holder),
        read_pressure=seam_overrides.get("read_pressure") or (lambda: None),
        reconcile=seam_overrides.get("reconcile") or (lambda: None),
        seed_chains=seam_overrides.get("seed_chains") or (lambda: None),
        expire=seam_overrides.get("expire") or (lambda: None),
        make_executor=seam_overrides.get("make_executor") or default_make_executor,
        spawn=seam_overrides.get("spawn") or spawn,
        sleep=sleep,
        poll_seconds=0.0,
        reclaim_leases=seam_overrides.get("reclaim_leases") or (lambda: None),
        reap_leases=seam_overrides.get("reap_leases") or (lambda: None),
        claim_master=seam_overrides.get("claim_master") or (lambda: None),
        release_master=seam_overrides.get("release_master") or (lambda: None),
    )
    if "executor_queues" in seam_overrides:
        seams = dataclasses.replace(seams, executor_queues=seam_overrides["executor_queues"])
    holder.append(LoopWorker(seams))
    return holder[0], built, handles


def test_supervisor_reclaims_dead_owner_leases_each_poll() -> None:
    reclaims: list[int] = []
    worker, _built, _ = _make_worker(
        script=[True],
        sleep=lambda _s: None,
        reclaim_leases=lambda: reclaims.append(1),
    )
    worker.run()
    assert reclaims, "the supervisor must sweep dead-owner loop leases on its poll cadence (#3571)"


def test_supervisor_survives_a_reclaim_error() -> None:
    def _boom() -> None:
        msg = "db hiccup"
        raise RuntimeError(msg)

    worker, _built, handles = _make_worker(
        script=[True],
        sleep=lambda _s: None,
        reclaim_leases=_boom,
    )
    worker.run()  # a reclaim error must never crash the supervisor
    assert all(handle.joined for handle in handles)


def test_reconciles_seeds_and_expires_before_starting_executors() -> None:
    order: list[str] = []
    worker, _built, _ = _make_worker(
        script=[True],  # one admitting poll spawns the pool, then the script ends
        sleep=lambda _s: None,
        reconcile=lambda: order.append("reconcile"),
        seed_chains=lambda: order.append("seed"),
        expire=lambda: order.append("expire"),
        make_executor=lambda q, wid: order.append("spawn") or _FakeExecutor(q, wid),
    )
    worker.run()
    # Expiry MUST run before any executor spawns, else a stale default-queue backlog
    # blind-fires the instant the worker starts (the load-jam class).
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


_THREE_LOOPS = ("loops",) * 3 + ("default",) * 3 + ("cheap",)


def test_pressure_ceiling_clamps_the_agent_lanes_but_keeps_all_three_queues() -> None:
    decision = AdmissionDecision(admit=True, reason="healthy", ceiling=2, braked=False)
    worker, built, _handles = _make_worker(
        script=[True],
        sleep=lambda _s: None,
        read_pressure=lambda: decision,
        executor_queues=_THREE_LOOPS,
    )
    worker.run()
    assert [executor.queue for executor in built] == ["loops", "loops", "loops", "default", "cheap"]


@pytest.mark.parametrize("ceiling", [2, 3, 4])
def test_pressure_ceiling_reserves_remaining_agent_lanes_for_coding(ceiling: int) -> None:
    queues = ("loops",) * 4 + ("default",) * 4 + ("cheap",)
    assert _bounded_executor_queues(queues, ceiling) == (
        *("loops",) * 4,
        *("default",) * (ceiling - 1),
        "cheap",
    )


def test_the_ceiling_never_clamps_the_loops_control_plane() -> None:
    """Loop timers are not agents: a 3-agent ceiling kept 1 loops executor where main ran 3."""
    bounded = _bounded_executor_queues(_THREE_LOOPS, 3)
    assert bounded.count("loops") == 3
    assert len(bounded) - bounded.count("loops") == 3


def test_a_one_agent_ceiling_keeps_dedicated_loops_executors() -> None:
    """One shared executor let a single coding task block every loop timer for hours."""
    assert _bounded_executor_queues(_THREE_LOOPS, 1) == ("loops", "loops", "loops", "default,cheap")


def test_a_brake_keeps_the_whole_loops_pool() -> None:
    denied = AdmissionDecision(admit=False, reason="host pressure", ceiling=1, braked=True, cause="load")
    worker, built, _handles = _make_worker(
        script=[True], sleep=lambda _s: None, read_pressure=lambda: denied, executor_queues=_THREE_LOOPS
    )
    worker.run()
    assert [executor.queue for executor in built] == ["loops", "loops", "loops", "cheap"]


def test_four_core_ceiling_starts_a_review_while_coding_is_active() -> None:
    pending = {"loops": ["timer/control"], "default": ["coding"], "cheap": ["reviewing"]}
    started: list[str] = []

    def spawn(executor: _FakeExecutor) -> _FakeHandle:
        started.extend(pending[queue].pop(0) for queue in executor.queue.split(",") if pending[queue])
        return _FakeHandle()

    decision = AdmissionDecision(admit=True, reason="four-core host", ceiling=2, braked=False)
    worker, built, _handles = _make_worker(
        script=[True],
        sleep=lambda _s: None,
        read_pressure=lambda: decision,
        spawn=spawn,
        executor_queues=("loops", "default", "default", "cheap"),
    )
    worker.run()

    assert [executor.queue for executor in built] == ["loops", "default", "cheap"]
    assert set(started) == {"timer/control", "coding", "reviewing"}


def test_braked_governor_keeps_control_loop_then_resumes_default_queue() -> None:
    verdicts = iter(
        [
            AdmissionDecision(admit=True, reason="healthy", ceiling=2, braked=False),
            AdmissionDecision(admit=False, reason="load 52 over 40 watermark", ceiling=2, braked=True, cause="load"),
            AdmissionDecision(admit=True, reason="healthy", ceiling=2, braked=False),
        ]
    )
    snapshots: list[int] = []
    worker, built, handles = _make_worker(
        script=[True, True, True],
        sleep=lambda _s: snapshots.append(len(built)),
        read_pressure=lambda: next(verdicts),
        executor_queues=("loops", "default", "default", "cheap"),
    )
    worker.run()
    assert snapshots[:3] == [3, 3, 4]
    assert handles[1].joined
    # The control handle stays live across the brake, then joins at final shutdown.
    assert handles[0].joined


def test_sustained_pressure_runs_control_and_cheap_review_but_not_coding() -> None:
    pending = {"loops": ["timer/control"], "default": ["coding"], "cheap": ["reviewing"]}
    completed: list[str] = []

    def spawn(executor: _FakeExecutor) -> _FakeHandle:
        completed.extend(pending[queue].pop(0) for queue in executor.queue.split(",") if pending[queue])
        return _FakeHandle()

    denied = AdmissionDecision(admit=False, reason="host pressure", ceiling=1, braked=True, cause="load")
    worker, built, _handles = _make_worker(
        script=[True, True, True],
        sleep=lambda _s: None,
        read_pressure=lambda: denied,
        spawn=spawn,
        executor_queues=("loops", "default", "default", "cheap"),
    )
    worker.run()

    assert completed == ["timer/control", "reviewing"]
    assert [executor.queue for executor in built] == ["loops", "cheap"]
    assert pending["default"] == ["coding"]


def test_token_brake_keeps_control_but_does_not_execute_cheap_agents() -> None:
    denied = AdmissionDecision(
        admit=False, reason="weekly quota exhausted", ceiling=2, braked=True, cause="weekly-quota"
    )
    worker, built, _handles = _make_worker(
        script=[True],
        sleep=lambda _s: None,
        read_pressure=lambda: denied,
        executor_queues=("loops", "default", "default", "cheap"),
    )
    worker.run()
    assert [executor.queue for executor in built] == ["loops"]


def test_spawns_host_scaled_loops_and_default_executors(monkeypatch: pytest.MonkeyPatch) -> None:
    # Patch BEFORE _make_worker so the WorkerSeams default_factory reads the host size.
    monkeypatch.setattr(worker_mod, "default_provision_concurrency", lambda: 3)
    worker, built, _ = _make_worker(script=[True], sleep=lambda _s: None)
    worker.run()
    queues = [executor.queue for executor in built]
    assert queues.count("loops") == 3
    assert queues.count("default") == 3


def test_a_preset_admitting_nothing_stops_and_joins_all_executors() -> None:
    worker, built, handles = _make_worker(script=[True, False], sleep=lambda _s: None)
    worker.run()
    assert all(not executor.running for executor in built)
    assert all(handle.joined for handle in handles)


def test_nothing_admitted_at_boot_keeps_the_worker_alive_without_executors_and_resumes() -> None:
    polls = 0

    def sleep(_seconds: float) -> None:
        nonlocal polls
        polls += 1
        if polls == 1:
            assert not built

    worker, built, handles = _make_worker(script=[False, True], sleep=sleep)
    worker.run()

    assert polls >= 2
    assert built
    assert all(not executor.running for executor in built)
    assert all(handle.joined for handle in handles)


def test_a_worker_admitting_nothing_continues_supervisor_maintenance() -> None:
    claims: list[int] = []
    reaps: list[int] = []
    reclaims: list[int] = []
    polls = 0

    def sleep(_seconds: float) -> None:
        nonlocal polls
        polls += 1
        if polls == 2:
            worker.request_stop()

    worker, built, _ = _make_worker(
        script=[False] * 5,
        sleep=sleep,
        claim_master=lambda: claims.append(1),
        reap_leases=lambda: reaps.append(1),
        reclaim_leases=lambda: reclaims.append(1),
    )
    worker.run()

    assert not built
    assert len(claims) == 3
    assert len(reaps) == 2
    assert len(reclaims) == 2


def test_quiescing_keeps_the_process_alive_and_re_admission_restarts_the_pool() -> None:
    # C1: the schedule boundary that re-admits work is driven from THIS process, so
    # quiescing must never exit — it stops the executors and keeps polling, and the next
    # admitting poll spawns a fresh pool unaided.
    worker, built, handles = _make_worker(script=[True, False, True], sleep=lambda _s: None)
    worker.run()
    per_pool = len(build_executor_queues())
    assert len(built) == 2 * per_pool  # the original pool, quiesced, then a fresh one
    assert all(handle.joined for handle in handles[:per_pool])


def test_stop_signal_tears_the_pool_down() -> None:
    worker, built, handles = None, None, None

    def sleep(_s: float) -> None:
        worker.request_stop()  # simulate SIGTERM arriving during a supervisor sleep

    worker, built, handles = _make_worker(script=[True] * 5, sleep=sleep)
    worker.run()
    assert all(not executor.running for executor in built)
    assert all(handle.joined for handle in handles)


def test_an_executor_that_survives_the_join_and_the_tick_kill_exits_non_zero() -> None:
    # A stuck executor that outlives the bounded stop would overlap the pool a restart
    # spawns, so shutdown refuses to exit clean over it.
    holder: list[LoopWorker] = []
    stuck: list[_StuckHandle] = []

    def spawn(_executor: _FakeExecutor) -> _StuckHandle:
        stuck.append(_StuckHandle())
        return stuck[-1]

    seams = WorkerSeams(
        read_admission=_scripted_reader([True] * 5, holder),
        reconcile=lambda: None,
        seed_chains=lambda: None,
        expire=lambda: None,
        make_executor=_FakeExecutor,
        spawn=spawn,
        kill_ticks=lambda: None,
        sleep=lambda _s: holder[0].request_stop(),
        poll_seconds=0.0,
        executor_queues=("loops",),
        reclaim_leases=lambda: None,
        reap_leases=lambda: None,
        claim_master=lambda: None,
        release_master=lambda: None,
    )
    holder.append(LoopWorker(seams))

    with pytest.raises(LoopWorkerExecutorStopError, match="executor pool did not stop"):
        holder[0].run()

    assert stuck
    assert all(handle.joined for handle in stuck)


def _liveness_seams(*, script, holder, spawn, make_executor, **overrides) -> WorkerSeams:
    return WorkerSeams(
        read_admission=_scripted_reader(script, holder),
        read_pressure=lambda: None,
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

    holder: list[LoopWorker] = []
    seams = _liveness_seams(script=[True], holder=holder, spawn=spawn, make_executor=make_executor)
    holder.append(LoopWorker(seams))
    holder[0].run()

    assert len(built) == 2  # the original dead executor + one respawn
    assert built[1].queue == "loops"


def test_repeated_executor_death_exits_the_worker_non_zero() -> None:
    # A crash-looping executor is a real fault — after the respawn budget is spent the
    # worker raises (exits non-zero) so the OS/container restarts it, never masks it.
    holder: list[LoopWorker] = []
    seams = _liveness_seams(
        script=[True] * 10,  # keeps admitting — only the crash path terminates run()
        holder=holder,
        spawn=lambda _e: _FakeHandle(alive=False),  # every executor is dead
        make_executor=_FakeExecutor,
        max_respawns=2,
    )
    holder.append(LoopWorker(seams))
    with pytest.raises(LoopWorkerExecutorCrashError):
        holder[0].run()


def test_shutdown_kills_in_flight_tick_process_groups() -> None:
    # A kill-switch flip / SIGTERM mid-tick tears down the executor thread that owned
    # the deadline, orphaning the tick subprocess with no deadline owner. The worker's
    # shutdown must SIGKILL any in-flight tick process group after the join timeout.
    deadlined_tick._LIVE_TICK_PGIDS.clear()  # process-global registry — isolate from other tests
    proc = spawn_session_leader(["sleep", "30"])  # stands in for an in-flight tick
    pgid = os.getpgid(proc.pid)
    deadlined_tick._register_tick_pgid(pgid)
    try:
        worker, _, _ = _make_worker(script=[], sleep=lambda _s: None)  # shut down at once
        worker.run()
        with contextlib.suppress(deadlined_tick.TimeoutExpired):
            proc.wait(timeout=5)
        assert not pid_alive(proc.pid)  # the orphaned group was killed, not left running
    finally:
        deadlined_tick._unregister_tick_pgid(pgid)
        deadlined_tick._killpg(pgid)


_DB_BACKEND = {
    "TASKS": {
        "default": {
            "BACKEND": "django_tasks_db.backend.DatabaseBackend",
            "QUEUES": ["default", "loops", "cheap"],
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
            holder: list[LoopWorker] = []
            seams = WorkerSeams(
                read_admission=_scripted_reader([True], holder),  # one admitting poll, then stop
                read_pressure=lambda: None,
                reconcile=lambda: None,
                seed_chains=lambda: None,
                make_executor=make_executor,
                spawn=lambda _e: _FakeHandle(),
                sleep=lambda _s: None,
                poll_seconds=0.0,
            )
            holder.append(LoopWorker(seams))
            holder[0].run()

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


def test_worker_prose_names_the_shipped_restart_policy() -> None:
    compose_file = Path(__file__).resolve().parents[2] / "deploy" / "docker-compose.yml"
    policy = yaml.safe_load(compose_file.read_text(encoding="utf-8"))["services"]["teatree-worker"]["restart"]
    source = inspect.getsource(worker_mod)
    assert "on-failure" not in source
    assert f"restart: {policy}" in source


class _LingeringHandle(_FakeHandle):
    """A thread that keeps running its current task after being told to stop."""

    def join(self, timeout: float | None = None) -> None:
        self.joined = True

    def finish(self) -> None:
        self._alive = False


def test_a_retiring_executor_holds_its_slot_until_its_thread_exits() -> None:
    """Refilling while a retired thread still runs would overshoot the ceiling it was retired for."""
    handles: list[_LingeringHandle] = []

    def spawn(_executor: _FakeExecutor) -> _LingeringHandle:
        handles.append(_LingeringHandle())
        return handles[-1]

    worker, built, _ = _make_worker(script=[], sleep=lambda _s: None, spawn=spawn)
    worker._resize_pool(("loops", "default"))
    worker._resize_pool(("loops",))
    assert built[1].running is False

    worker._resize_pool(("loops", "cheap"))
    assert [executor.queue for executor in built] == ["loops", "default"]

    handles[1].finish()
    worker._resize_pool(("loops", "cheap"))
    assert [executor.queue for executor in built] == ["loops", "default", "cheap"]
