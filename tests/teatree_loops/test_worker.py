"""teatree.loops.worker — the singleton executor pool + supervisor (#1796).

Pure supervision/lifecycle logic with injected collaborators — no real threads, DB,
or clock. Verifies startup reconciliation, the executor split (a host-scaled ``loops``
pool + agent pools sized to the headless lane widths), that a preset admitting zero loops
quiesces the pool while the PROCESS stays alive, and that a stop signal tears the pool down.
"""

import asyncio
import contextlib
import dataclasses
import datetime as dt
import functools
import inspect
import os
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING
from unittest import mock

import pytest
import yaml
from claude_agent_sdk import ClaudeAgentOptions
from django.db import OperationalError
from django.tasks import TaskResultStatus
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from django_tasks_db.management.commands import db_worker
from django_tasks_db.models import DBTaskResult

from teatree.agents import live_mailbox
from teatree.agents.live_client import LiveClient
from teatree.agents.runner_heartbeat import HeartbeatRuntime, drain_reason_closing_connection, drive_with_heartbeat
from teatree.agents.runner_interruption import _record_stuck_outcome
from teatree.agents.runner_watchdog import LoopWatchdog, TaskUsage
from teatree.core import agent_admission, managers_task_claim
from teatree.core.admission.lanes import ANSWERING_LANE_WIDTH
from teatree.core.admission_governor import (
    AdmissionCeiling,
    AdmissionDecision,
    QuotaSignal,
    decide_admission,
    read_machine_signal,
)
from teatree.core.agent_admission import LaneWidths, agent_admission_verdict
from teatree.core.managers_admission import ADMITTED_INFLIGHT_WINDOW
from teatree.core.modelkit.phases import MAINTENANCE_PHASES
from teatree.core.models import ConfigSetting, Session, Task, Ticket
from teatree.core.tasks import refresh_followup_snapshot
from teatree.loops import deadlined_tick
from teatree.loops import worker as worker_mod
from teatree.loops.enable_verdict import FleetAdmission
from teatree.loops.executor_thread import Executor, Handle, build_executor, spawn_executor_thread
from teatree.loops.worker import (
    DEFAULT_QUEUE_FLOOR,
    LOOPS_EXECUTOR_FLOOR,
    LoopWorker,
    LoopWorkerExecutorCrashError,
    LoopWorkerExecutorStopError,
    WorkerSeams,
    agent_executor_queues,
    loops_executor_count,
)
from teatree.utils import host_pressure_sampler
from teatree.utils.host_pressure import read_host_pressure
from teatree.utils.host_pressure_sampler import HostSampler
from teatree.utils.run import spawn_session_leader
from teatree.utils.singleton import pid_alive
from teatree.utils.thread_db import close_thread_db_connections
from tests._file_backed_db import file_backed_default_database
from tests.teatree_agents._sdk_fake import InterruptibleSession, OneSessionHarness

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Sequence
    from contextlib import AbstractContextManager

    from teatree.agents.runner_stream import HarnessOutcome


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
        self.degraded = False

    def is_alive(self) -> bool:
        return self._alive

    def join(self, timeout: float | None = None) -> None:
        self.joined = True
        self._alive = False


class _StuckHandle(_FakeHandle):
    def join(self, timeout: float | None = None) -> None:
        self.joined = True


class _RunningHandle(_FakeHandle):
    def __init__(self, executor: Executor) -> None:
        super().__init__()
        self.executor = executor

    def is_alive(self) -> bool:
        return self.executor.running


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
        publish_host_pressure=seam_overrides.get("publish_host_pressure") or (lambda: None),
        start_live_ingress=seam_overrides.get("start_live_ingress") or (lambda: None),
        read_agent_queues=seam_overrides.get("read_agent_queues") or (lambda: _AGENTS),
    )
    if "loops_executors" in seam_overrides:
        seams = dataclasses.replace(seams, loops_executors=seam_overrides["loops_executors"])
    holder.append(LoopWorker(seams))
    return holder[0], built, handles


def test_supervisor_poll_produces_headless_linux_feed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "meminfo").write_text("MemAvailable: 8388608 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n")
    (proc / "vmstat").write_text("pswpin 0\npswpout 0\n")
    (proc / "loadavg").write_text("1.0 0.5 0.25 1/100 100\n")
    target = tmp_path / "host-pressure.json"
    monkeypatch.setenv("TEATREE_HOST_OS", "Linux")
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    sampler = HostSampler(proc_root=proc, path=target)
    worker, _, _ = _make_worker(
        script=[False], sleep=lambda _seconds: None, publish_host_pressure=sampler.publish_if_due
    )

    worker.run()

    assert read_host_pressure(path=target) is not None


def test_supervisor_poll_does_not_overwrite_darwin_feed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "host-pressure.json"
    target.write_bytes(b"host feed")
    monkeypatch.setenv("TEATREE_HOST_OS", "Darwin")
    worker, _, _ = _make_worker(
        script=[False],
        sleep=lambda _seconds: None,
        publish_host_pressure=HostSampler(proc_root=tmp_path, path=target).publish_if_due,
    )

    worker.run()

    assert target.read_bytes() == b"host feed"


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


def test_supervisor_survives_a_host_pressure_publish_error() -> None:
    def fail_publish() -> None:
        reason = "host feed unavailable"
        raise OSError(reason)

    worker, _built, handles = _make_worker(
        script=[True], sleep=lambda _seconds: None, publish_host_pressure=fail_publish
    )
    worker.run()
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
    assert order.count("spawn") == loops_executor_count() + len(_AGENTS)


def test_the_live_socket_is_published_before_anything_else_starts() -> None:
    order: list[str] = []
    worker, _built, _ = _make_worker(
        script=[True],
        sleep=lambda _s: None,
        start_live_ingress=lambda: order.append("live"),
        reconcile=lambda: order.append("reconcile"),
    )
    worker.run()
    assert order[:2] == ["live", "reconcile"]


def test_a_worker_running_nothing_answers_live_list_with_no_sessions() -> None:
    worker_mod._start_live_ingress()

    assert LiveClient().sessions() == []
    assert len([peer for peer in live_mailbox.shared_broker().identities() if peer["harness"] == "orchestrator"]) == 1
    worker_mod._start_live_ingress()
    assert len([peer for peer in live_mailbox.shared_broker().identities() if peer["harness"] == "orchestrator"]) == 1
    assert WorkerSeams().start_live_ingress is worker_mod._start_live_ingress


@pytest.mark.skipif(sys.platform != "linux", reason="Linux host sampler")
def test_default_worker_seam_publishes_host_pressure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "host-pressure.json"
    monkeypatch.setenv("T3_HOST_PRESSURE_PATH", str(target))
    monkeypatch.setattr(host_pressure_sampler, "proc_reads_the_host", lambda: True)

    WorkerSeams().publish_host_pressure()

    reading = read_host_pressure(path=target)
    assert reading is not None
    assert reading.cores >= 1


def test_a_live_ingress_that_cannot_start_never_stops_the_worker(caplog: pytest.LogCaptureFixture) -> None:
    with mock.patch.object(live_mailbox, "shared_broker", side_effect=OSError("read-only volume")):
        worker_mod._start_live_ingress()

    assert "Live ingress could not start" in caplog.text


def test_the_loops_pool_scales_with_host_cores(monkeypatch: pytest.MonkeyPatch) -> None:
    # Two slow ticks no longer stall every OTHER loop on a bigger box.
    monkeypatch.setattr(worker_mod, "default_provision_concurrency", lambda: 4)
    assert loops_executor_count() == 4


def test_the_loops_pool_is_floored_at_two_on_a_small_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_mod, "default_provision_concurrency", lambda: 1)
    assert loops_executor_count() == LOOPS_EXECUTOR_FLOOR == 2


def _widths(expensive: int, review: int) -> LaneWidths:
    return LaneWidths(
        ceiling=AdmissionCeiling(cores=expensive, per_core=1.0, pace=None),
        review=review,
        maintenance=len(MAINTENANCE_PHASES),
    )


@pytest.mark.parametrize(("expensive", "review"), [(1, 1), (3, 2), (15, 2)])
def test_each_lane_seat_gets_one_executor_on_its_queue(expensive: int, review: int) -> None:
    assert agent_executor_queues(_widths(expensive, review)) == ("default",) * expensive + ("cheap",) * (
        review + 2 + ANSWERING_LANE_WIDTH + len(MAINTENANCE_PHASES)
    )


def test_unread_lane_widths_keep_the_host_sized_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_mod, "default_provision_concurrency", lambda: 1)
    assert agent_executor_queues(None) == ("default",) * DEFAULT_QUEUE_FLOOR + ("cheap",) * (
        3 + ANSWERING_LANE_WIDTH + len(MAINTENANCE_PHASES)
    )
    monkeypatch.setattr(worker_mod, "default_provision_concurrency", lambda: 4)
    assert agent_executor_queues(None) == ("default",) * 4 + ("cheap",) * (
        3 + ANSWERING_LANE_WIDTH + len(MAINTENANCE_PHASES)
    )


def test_a_lane_width_read_failure_keeps_the_host_sized_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent_admission, "headless_lane_widths", mock.Mock(side_effect=RuntimeError("db down")))
    assert worker_mod._read_agent_queues() == agent_executor_queues(None)


def test_the_lane_widths_never_size_the_loops_control_plane() -> None:
    """Loop timers are not agents: a 3-agent ceiling kept 1 loops executor where main ran 3."""
    one_seat = agent_executor_queues(_widths(1, 1))
    worker, built, _handles = _make_worker(
        script=[True], sleep=lambda _s: None, loops_executors=3, read_agent_queues=lambda: one_seat
    )
    worker.run()
    assert [executor.queue for executor in built] == [
        *["loops"] * 3,
        "default",
        *["cheap"] * (3 + ANSWERING_LANE_WIDTH + len(MAINTENANCE_PHASES)),
    ]


def test_a_brake_keeps_the_whole_loops_pool() -> None:
    denied = AdmissionDecision(admit=False, reason="host pressure", ceiling=1, braked=True, cause="load")
    worker, built, _handles = _make_worker(
        script=[True], sleep=lambda _s: None, read_pressure=lambda: denied, loops_executors=3
    )
    worker.run()
    assert [executor.queue for executor in built] == ["loops", "loops", "loops", "cheap"]


def test_a_machine_brake_keeps_one_executor_per_cheap_seat() -> None:
    denied = AdmissionDecision(admit=False, reason="host pressure", ceiling=8, braked=True, cause="load")
    lanes = agent_executor_queues(_widths(7, 2))
    worker, built, _handles = _make_worker(
        script=[True],
        sleep=lambda _s: None,
        read_pressure=lambda: denied,
        loops_executors=1,
        read_agent_queues=lambda: lanes,
    )
    worker.run()
    assert [executor.queue for executor in built] == [
        "loops",
        *["cheap"] * (4 + ANSWERING_LANE_WIDTH + len(MAINTENANCE_PHASES)),
    ]


def test_four_core_ceiling_starts_a_review_while_coding_is_active() -> None:
    pending = {"loops": ["timer/control"], "default": ["coding"], "cheap": ["reviewing"]}
    started: list[str] = []

    def spawn(executor: _FakeExecutor) -> _FakeHandle:
        started.extend(pending[queue].pop(0) for queue in executor.queue.split(",") if pending[queue])
        return _FakeHandle()

    decision = AdmissionDecision(admit=True, reason="four-core host", ceiling=2, braked=False)
    four_core = agent_executor_queues(_widths(1, 1))
    worker, built, _handles = _make_worker(
        script=[True],
        sleep=lambda _s: None,
        read_pressure=lambda: decision,
        spawn=spawn,
        loops_executors=3,
        read_agent_queues=lambda: four_core,
    )
    worker.run()

    assert [executor.queue for executor in built] == [
        *["loops"] * 3,
        "default",
        *["cheap"] * (3 + ANSWERING_LANE_WIDTH + len(MAINTENANCE_PHASES)),
    ]
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
        loops_executors=2,
        read_agent_queues=lambda: ("default", "cheap"),
    )
    worker.run()
    assert snapshots[:3] == [4, 4, 5]
    assert built[2].queue == "default"
    assert handles[2].joined
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
        loops_executors=3,
    )
    worker.run()

    assert completed == ["timer/control", "reviewing"]
    assert [executor.queue for executor in built] == ["loops", "loops", "loops", "cheap"]
    assert pending["default"] == ["coding"]


def test_token_brake_keeps_control_but_does_not_execute_cheap_agents() -> None:
    denied = AdmissionDecision(
        admit=False, reason="weekly quota exhausted", ceiling=2, braked=True, cause="weekly-quota"
    )
    worker, built, _handles = _make_worker(
        script=[True], sleep=lambda _s: None, read_pressure=lambda: denied, loops_executors=3
    )
    worker.run()
    assert [executor.queue for executor in built] == ["loops", "loops", "loops"]


#: Three coding seats and one review seat.
_AGENTS = ("default",) * 3 + ("cheap",)


@pytest.mark.parametrize("cause", ["swap", "weekly-quota"])
def test_a_brake_keeps_the_whole_control_plane_so_one_slow_tick_cannot_starve_the_rest(cause: str) -> None:
    # Braked to ONE loops executor, a 300 s resource_pressure tick held every other timer (#98).
    denied = AdmissionDecision(admit=False, reason="braked", ceiling=1, braked=True, cause=cause)
    worker, built, _handles = _make_worker(
        script=[True], sleep=lambda _s: None, read_pressure=lambda: denied, loops_executors=3
    )
    worker.run()

    assert [executor.queue for executor in built].count("loops") == 3


def test_spawns_host_scaled_loops_and_lane_sized_agent_executors(monkeypatch: pytest.MonkeyPatch) -> None:
    # Patch BEFORE _make_worker so the WorkerSeams default_factory reads the host size.
    monkeypatch.setattr(worker_mod, "default_provision_concurrency", lambda: 5)
    worker, built, _ = _make_worker(script=[True], sleep=lambda _s: None)
    worker.run()
    queues = [executor.queue for executor in built]
    assert queues.count("loops") == 5
    assert queues.count("default") == _AGENTS.count("default")


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
    per_pool = loops_executor_count() + len(_AGENTS)
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


def test_critical_kernel_memory_pressure_retires_the_cheap_lane_but_keeps_control() -> None:
    denied = AdmissionDecision(admit=False, reason="kernel critical", ceiling=1, braked=True, cause="memory-pressure")
    worker, built, _handles = _make_worker(
        script=[True], sleep=lambda _s: None, read_pressure=lambda: denied, loops_executors=3
    )
    worker.run()

    assert [executor.queue for executor in built] == ["loops", "loops", "loops"]


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
        loops_executors=1,
        read_agent_queues=lambda: (),
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
        claim_master=lambda: None,
        release_master=lambda: None,
        reclaim_leases=lambda: None,
        reap_leases=lambda: None,
        start_live_ingress=lambda: None,
        sleep=lambda _s: None,
        poll_seconds=0.0,
        loops_executors=1,
        read_agent_queues=lambda: (),
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


def test_a_healthy_respawn_refunds_its_crash_budget_after_ten_minutes() -> None:
    clock = [0.0]
    handles: list[_FakeHandle] = []

    def spawn(_executor: _FakeExecutor) -> _FakeHandle:
        handle = _FakeHandle(alive=bool(handles))
        handles.append(handle)
        return handle

    seams = WorkerSeams(
        make_executor=_FakeExecutor,
        spawn=spawn,
        monotonic=lambda: clock[0],
        loops_executors=1,
        read_agent_queues=lambda: (),
    )
    worker = LoopWorker(seams)
    worker._resize_pool(("loops",))
    for expected in (1, 2, 3):
        worker._respawn_dead_executors()
        assert worker._slots[0].respawns == expected
        handles[-1]._alive = False
    handles[-1]._alive = True

    clock[0] = 599.0
    worker._respawn_dead_executors()
    assert worker._slots[0].respawns == 3

    clock[0] = 600.0
    worker._respawn_dead_executors()
    assert worker._slots[0].respawns == 0

    handles[-1]._alive = False
    worker._respawn_dead_executors()
    assert worker._slots[0].respawns == 1


def test_a_non_zero_exit_waits_only_the_checkpoint_grace(caplog: pytest.LogCaptureFixture) -> None:
    clock = [0.0]
    reasons: list[str] = []
    holder: list[LoopWorker] = []

    def spawn(executor: _FakeExecutor) -> _FakeHandle:
        return _FakeHandle(alive=False) if executor.queue == "loops" else _StuckHandle()

    seams = dataclasses.replace(
        _liveness_seams(script=[True] * 10, holder=holder, spawn=spawn, make_executor=_FakeExecutor),
        read_agent_queues=lambda: ("default",),
        max_respawns=0,
        monotonic=lambda: clock[0],
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        begin_exit=reasons.append,
    )
    holder.append(LoopWorker(seams))

    with pytest.raises(LoopWorkerExecutorStopError):
        holder[0].run()

    assert reasons[0].startswith("this worker is exiting non-zero: executor for queue loops (slot 0)")
    assert len(reasons) == 1
    assert clock[0] == pytest.approx(300.0)
    assert "still active after checkpoint grace" in caplog.text


def test_sigterm_during_checkpoint_grace_exits_early() -> None:
    clock = [0.0]
    holder: list[LoopWorker] = []

    def sleep(seconds: float) -> None:
        clock[0] += seconds
        if clock[0] >= 2.0:
            holder[0].request_stop()

    seams = dataclasses.replace(
        _liveness_seams(
            script=[True] * 10,
            holder=holder,
            spawn=lambda executor: _FakeHandle(alive=False) if executor.queue == "loops" else _StuckHandle(),
            make_executor=_FakeExecutor,
        ),
        read_agent_queues=lambda: ("default",),
        max_respawns=0,
        monotonic=lambda: clock[0],
        sleep=sleep,
        begin_exit=lambda _reason: None,
    )
    holder.append(LoopWorker(seams))

    with pytest.raises(LoopWorkerExecutorStopError):
        holder[0].run()

    assert clock[0] == pytest.approx(2.0)


def test_checkpoint_grace_keeps_master_lease_fresh() -> None:
    clock = [0.0]
    claims: list[float] = []
    holder: list[LoopWorker] = []
    seams = dataclasses.replace(
        _liveness_seams(
            script=[True] * 10,
            holder=holder,
            spawn=lambda executor: _FakeHandle(alive=False) if executor.queue == "loops" else _StuckHandle(),
            make_executor=_FakeExecutor,
        ),
        read_agent_queues=lambda: ("default",),
        max_respawns=0,
        monotonic=lambda: clock[0],
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        claim_master=lambda: claims.append(clock[0]),
        master_refresh_seconds=60.0,
        begin_exit=lambda _reason: None,
    )
    holder.append(LoopWorker(seams))

    with pytest.raises(LoopWorkerExecutorStopError):
        holder[0].run()

    assert len(claims) >= 6
    assert max(later - earlier for earlier, later in pairwise(claims)) <= 60.0
    assert managers_task_claim.process_exit.reason == ""


def test_real_lock_tolerant_handle_publishes_degraded_health() -> None:
    clock = [0.0]
    degraded_ready = threading.Event()
    release = threading.Event()
    beats: list[tuple[str, bool]] = []
    holder: list[LoopWorker] = []

    class LockedExecutor(_FakeExecutor):
        calls = 0

        def run(self) -> None:
            self.calls += 1
            if self.calls <= 2:
                message = "database is locked"
                raise OperationalError(message)
            release.wait(timeout=10)

    def backoff(_seconds: float) -> None:
        if executor.calls == 1:
            clock[0] = 601.0
        else:
            degraded_ready.set()
            release.wait(timeout=10)

    def supervisor_sleep(_seconds: float) -> None:
        assert degraded_ready.wait(timeout=10)

    polls = [0]

    def read_admission() -> FleetAdmission:
        polls[0] += 1
        if polls[0] == 1:
            return FleetAdmission.ADMITS
        release.set()
        holder[0].request_stop()
        return FleetAdmission.NONE

    executor = LockedExecutor("loops", "worker-0-loops")
    seams = dataclasses.replace(
        _liveness_seams(
            script=[True],
            holder=holder,
            spawn=lambda _executor: spawn_executor_thread(executor, sleep=backoff, monotonic=lambda: clock[0]),
            make_executor=lambda _queue, _id: executor,
        ),
        sleep=supervisor_sleep,
        read_admission=read_admission,
        publish_health=lambda admission, *, active: beats.append((admission, active)),
    )
    holder.append(LoopWorker(seams))
    try:
        holder[0].run()
    finally:
        release.set()

    assert beats == [("degraded", False)]


def test_an_unexpected_supervisor_failure_still_announces_a_checkpoint() -> None:
    reasons: list[str] = []
    polls = 0

    def read_admission() -> FleetAdmission:
        nonlocal polls
        polls += 1
        if polls == 1:
            return FleetAdmission.ADMITS
        message = "supervisor failed"
        raise RuntimeError(message)

    holder: list[LoopWorker] = []
    seams = dataclasses.replace(
        _liveness_seams(script=[], holder=holder, spawn=_RunningHandle, make_executor=_FakeExecutor),
        read_admission=read_admission,
        begin_exit=reasons.append,
    )
    holder.append(LoopWorker(seams))

    with pytest.raises(RuntimeError, match="supervisor failed"):
        holder[0].run()

    assert reasons[0] == "this worker is exiting non-zero: supervisor failed with RuntimeError"


def test_a_stop_request_never_announces_a_checkpoint() -> None:
    reasons: list[str] = []
    holder: list[LoopWorker] = []
    seams = dataclasses.replace(
        _liveness_seams(script=[True] * 5, holder=holder, spawn=_RunningHandle, make_executor=_FakeExecutor),
        read_agent_queues=lambda: ("default",),
        sleep=lambda _seconds: holder[0].request_stop(),
        begin_exit=reasons.append,
    )
    holder.append(LoopWorker(seams))

    holder[0].run()

    assert reasons == []


@contextlib.contextmanager
def _lock_first_six_transactions(
    alias: str, *, attempts: list[int], original: "Callable[[str], AbstractContextManager[None]]"
) -> "Generator[None]":
    attempts[0] += 1
    if attempts[0] <= 6:
        msg = "database is locked"
        raise OperationalError(msg)
    with original(alias):
        yield


class TestLockedExecutorKeepsAgentClaim(TransactionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.enterContext(file_backed_default_database())

    def test_six_locked_loop_attempts_keep_the_agent_claim_and_serve_work(self) -> None:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR)
        session = Session.objects.create(ticket=ticket)
        agent = Task.objects.create(ticket=ticket, session=session, phase="coding", status=Task.Status.PENDING)
        agent.claim(claimed_by="worker-A", lease_seconds=900)
        claim_generation = (agent.claimed_by, agent.claimed_by_session, agent.claimed_at)
        attempts = [0]
        built: list[str] = []
        holder: list[LoopWorker] = []
        enqueued = False

        def make_executor(queue: str, worker_id: str) -> Executor:
            built.append(queue)
            return build_executor(queue, worker_id) if queue == "loops" else _FakeExecutor(queue, worker_id)

        def spawn(executor: Executor) -> Handle:
            if isinstance(executor, db_worker.Worker):
                return spawn_executor_thread(executor, sleep=lambda _seconds: time.sleep(0.001))
            return _RunningHandle(executor)

        def supervisor_sleep(_seconds: float) -> None:
            nonlocal enqueued
            if attempts[0] >= 6 and not enqueued:
                refresh_followup_snapshot.using(queue_name="loops").enqueue()
                enqueued = True
            if DBTaskResult.objects.filter(queue_name="loops", status=TaskResultStatus.SUCCESSFUL).exists():
                holder[0].request_stop()
            time.sleep(0.01)

        seams = dataclasses.replace(
            _liveness_seams(script=[True] * 1000, holder=holder, spawn=spawn, make_executor=make_executor),
            read_agent_queues=lambda: ("default",),
            sleep=supervisor_sleep,
        )
        holder.append(LoopWorker(seams))
        original_transaction = db_worker.exclusive_transaction
        locked_transactions = functools.partial(
            _lock_first_six_transactions, attempts=attempts, original=original_transaction
        )
        with (
            override_settings(**_DB_BACKEND),
            mock.patch.object(db_worker, "exclusive_transaction", locked_transactions),
        ):
            holder[0].run()

        agent.refresh_from_db()
        assert DBTaskResult.objects.get(queue_name="loops").status == TaskResultStatus.SUCCESSFUL
        assert attempts[0] >= 7
        assert built == ["loops", "default"]
        assert agent.status == Task.Status.CLAIMED
        assert (agent.claimed_by, agent.claimed_by_session, agent.claimed_at) == claim_generation


class TestAWorkerCrashCheckpointsTheAgent(TransactionTestCase):
    def test_a_non_lock_crash_parks_the_run_with_its_cause(self) -> None:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR)
        session = Session.objects.create(ticket=ticket)
        agent = Task.objects.create(ticket=ticket, session=session, phase="coding", status=Task.Status.PENDING)
        agent.claim(claimed_by="worker-A", lease_seconds=900)
        session = InterruptibleSession([], session_id="0f1e2d3c-4b5a-4968-8776-655443322110")
        runtime = HeartbeatRuntime(
            watchdog=LoopWatchdog(max_runtime_seconds=5, max_turns=0, max_cost_usd=0.0),
            heartbeat_interval=0.01,
            sample_usage=lambda _task: TaskUsage(turns=0, cost_usd=0.0),
            renew_lease=lambda task: None,
            drain_reason=drain_reason_closing_connection,
        )
        outcomes: list[HarnessOutcome] = []
        checkpoint_ready = threading.Event()
        park_done = threading.Event()
        crashes: list[BaseException] = []

        class FailingLoopExecutor(_FakeExecutor):
            def run(self) -> None:
                message = "disk I/O error"
                raise OperationalError(message)

        class AgentThread(threading.Thread):
            @property
            def degraded(self) -> bool:
                return False

        def make_executor(queue: str, worker_id: str) -> _FakeExecutor:
            if queue == "loops":
                return FailingLoopExecutor(queue, worker_id)
            return _FakeExecutor(queue, worker_id)

        def drive_agent() -> None:
            try:
                outcome = asyncio.run(
                    drive_with_heartbeat(agent, "p", ClaudeAgentOptions(), OneSessionHarness(session), runtime=runtime)
                )
                outcomes.append(outcome)
                checkpoint_ready.set()
                park_done.wait(timeout=10)
            finally:
                close_thread_db_connections()

        def spawn(executor: _FakeExecutor) -> Handle:
            if executor.queue == "loops":
                return spawn_executor_thread(executor)
            thread = AgentThread(target=drive_agent)
            thread.start()
            return thread

        holder: list[LoopWorker] = []
        seams = dataclasses.replace(
            _liveness_seams(
                script=[True] * 10,
                holder=holder,
                spawn=spawn,
                make_executor=make_executor,
                max_respawns=3,
            ),
            read_agent_queues=lambda: ("default",),
            sleep=lambda _seconds: time.sleep(0.01),
        )
        holder.append(LoopWorker(seams))

        with (
            mock.patch.object(managers_task_claim.process_exit, "reason", ""),
            mock.patch.object(managers_task_claim, "worker_is_quiescing", return_value=False),
            mock.patch.object(managers_task_claim, "generation_admission_refusal", return_value=""),
            mock.patch.object(threading, "excepthook", lambda args: crashes.append(args.exc_value)),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            future = pool.submit(holder[0].run)
            try:
                assert checkpoint_ready.wait(timeout=10)
                assert outcomes[0].checkpointed
                _record_stuck_outcome(agent, outcomes[0], stuck_reason=outcomes[0].stuck_reason or "")
            finally:
                park_done.set()
            with pytest.raises(LoopWorkerExecutorCrashError):
                future.result(timeout=10)
            assert managers_task_claim.process_exit.reason == ""

        agent.refresh_from_db()
        assert agent.status == Task.Status.PENDING
        assert agent.claimed_by == ""
        assert len(crashes) == 4
        assert all(isinstance(error, OperationalError) and "disk I/O error" in str(error) for error in crashes)
        assert agent.attempts.latest("pk").error == (
            "limit_parked: checkpoint: this worker is exiting non-zero: executor for queue loops (slot 0) died 4 times"
        )


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


_PACED_QUOTA = QuotaSignal(
    fresh=True,
    all_accounts_exhausted=False,
    weekly_utilization=0.1,
    short_utilization=0.1,
    seconds_to_weekly_reset=7 * 24 * 3600 * 0.5,
)


class TestEverySeatTheVerdictAdmitsHasAnExecutor(TestCase):
    """A seat with no executor waits in the queue until its window lapses and the drain books it again."""

    def setUp(self) -> None:
        from django.db.models.signals import post_save  # noqa: PLC0415 - deferred: local import

        from teatree.core.signals import _auto_enqueue_task  # noqa: PLC0415 - deferred: local import

        post_save.disconnect(_auto_enqueue_task, sender=Task, dispatch_uid="auto_enqueue_task")
        self.addCleanup(post_save.connect, _auto_enqueue_task, sender=Task, dispatch_uid="auto_enqueue_task")
        quota = mock.patch.object(agent_admission, "read_quota_signal", return_value=_PACED_QUOTA)
        quota.start()
        self.addCleanup(quota.stop)
        self.ticket = Ticket.objects.create()
        self.session = Session.objects.create(ticket=self.ticket)

    def _admit(self, phase: str, rows: int = 20) -> list[Task]:
        verdict = agent_admission_verdict()
        pending = [
            Task.objects.create(ticket=self.ticket, session=self.session, status=Task.Status.PENDING, phase=phase)
            for _ in range(rows)
        ]
        return [row for row in pending if verdict.admit(row.pk, phase, at="test")]

    def _executors(self) -> Counter[str]:
        built: list[_FakeExecutor] = []
        holder: list[LoopWorker] = []

        def make_executor(queue: str, worker_id: str) -> _FakeExecutor:
            built.append(_FakeExecutor(queue, worker_id))
            return built[-1]

        seams = WorkerSeams(
            read_admission=_scripted_reader([True], holder),
            read_pressure=lambda: decide_admission(quota=_PACED_QUOTA, machine=read_machine_signal()),
            reconcile=lambda: None,
            seed_chains=lambda: None,
            expire=lambda: None,
            make_executor=make_executor,
            spawn=lambda _executor: _FakeHandle(),
            kill_ticks=lambda: None,
            reclaim_leases=lambda: None,
            reap_leases=lambda: None,
            claim_master=lambda: None,
            release_master=lambda: None,
            publish_health=lambda _admission, *, active: None,
            sleep=lambda _s: None,
            poll_seconds=0.0,
        )
        holder.append(LoopWorker(seams))
        holder[0].run()
        return Counter(executor.queue for executor in built)

    def test_every_admitted_coding_seat_gets_a_default_executor(self) -> None:
        for factor in (0.25, 0.5, 1.0, 2.0):
            with self.subTest(factor=factor):
                ConfigSetting.objects.set_value("admission_write_concurrency_per_core", factor)
                Task.objects.all().delete()
                seated = self._admit("coding")

                assert self._executors()["default"] == len(seated)

    def test_every_admitted_cheap_seat_gets_a_cheap_executor(self) -> None:
        ConfigSetting.objects.set_value("admission_write_concurrency_per_core", 1.0)
        reviews = self._admit("reviewing")
        retiring = self._admit("shipping")

        assert len(reviews) == 2
        assert len(retiring) == 1
        assert len(reviews) + len(retiring) == 3
        assert self._executors()["cheap"] == 4 + ANSWERING_LANE_WIDTH + len(MAINTENANCE_PHASES)

    def test_every_admitted_answering_seat_gets_a_cheap_executor(self) -> None:
        answering = self._admit("answering")

        assert len(answering) == ANSWERING_LANE_WIDTH
        assert self._executors()["cheap"] == 4 + len(answering) + len(MAINTENANCE_PHASES)

    def test_a_wider_review_lane_keeps_a_retiring_executor(self) -> None:
        ConfigSetting.objects.set_value("cheap_phase_admission_ceiling", 3)
        seated = self._admit("reviewing")

        assert len(seated) == 3
        assert self._executors()["cheap"] == len(seated) + 2 + ANSWERING_LANE_WIDTH + len(MAINTENANCE_PHASES)

    def test_review_seats_and_executors_sit_beside_a_full_coding_lane(self) -> None:
        coding = self._admit("coding")
        reviews = self._admit("reviewing")
        executors = self._executors()

        assert (len(coding), len(reviews)) == (4, 2)
        assert (executors["default"], executors["cheap"]) == (4, 4 + ANSWERING_LANE_WIDTH + len(MAINTENANCE_PHASES))

    def test_a_seat_never_lapses_into_a_second_booking(self) -> None:
        ConfigSetting.objects.set_value("admission_write_concurrency_per_core", 1.0)
        seated = self._admit("coding")
        running = [row.pk for row in seated[: self._executors()["default"]]]
        Task.objects.filter(pk__in=running).update(
            status=Task.Status.CLAIMED, lease_expires_at=timezone.now() + dt.timedelta(hours=1)
        )
        Task.objects.filter(pk__in=[row.pk for row in seated]).update(
            admitted_at=timezone.now() - ADMITTED_INFLIGHT_WINDOW * 2
        )

        verdict = agent_admission_verdict()
        rebooked = [row.pk for row in seated if verdict.admit(row.pk, "coding", at="after the window")]

        assert rebooked == []


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
