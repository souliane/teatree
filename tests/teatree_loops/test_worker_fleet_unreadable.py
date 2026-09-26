"""The worker crashes NON-ZERO on a persistent fleet-verdict READ FAILURE (F7).

A read that RAISES is UNREADABLE, not a deliberate stop. Collapsed to "admits nothing" it
would quiesce the pool silently, so a transient DB error would leave the factory parked
with nothing to restart it. Now UNREADABLE is retried a few polls (a blip recovers) then
crashes so ``restart: on-failure`` restarts the worker; a posture admitting zero loops
parks the pool with the process ALIVE, which is a stop rather than a fault.
"""

import pytest

from teatree.loops.enable_verdict import FleetAdmission
from teatree.loops.worker import FleetAdmissionUnreadableError, LoopWorker, WorkerSeams


class _FakeExecutor:
    def __init__(self, queue: str, worker_id: str) -> None:
        self.queue = queue
        self.running = True

    def run(self) -> None:
        pass


class _FakeHandle:
    def __init__(self) -> None:
        self.joined = False

    def is_alive(self) -> bool:
        return not self.joined

    def join(self, timeout: float | None = None) -> None:
        self.joined = True


def _worker(
    verdicts: list[FleetAdmission],
    *,
    max_unreadable_polls: int = 3,
    health_beats: list[tuple[str, bool]] | None = None,
):
    handles: list[_FakeHandle] = []
    remaining = iter(verdicts)
    worker: LoopWorker | None = None

    def spawn(_executor: object) -> _FakeHandle:
        handle = _FakeHandle()
        handles.append(handle)
        return handle

    def read_admission() -> FleetAdmission:
        verdict = next(remaining, None)
        if verdict is None:
            assert worker is not None
            worker.request_stop()  # the scripted run is over — nothing else ends the loop
            return FleetAdmission.NONE
        return verdict

    seams = WorkerSeams(
        read_admission=read_admission,
        read_pressure=lambda: None,
        reconcile=lambda: None,
        seed_chains=lambda: None,
        expire=lambda: None,
        make_executor=_FakeExecutor,
        spawn=spawn,
        kill_ticks=lambda: None,
        reclaim_leases=lambda: None,
        claim_master=lambda: None,
        release_master=lambda: None,
        sleep=lambda _s: None,
        poll_seconds=0.0,
        max_unreadable_polls=max_unreadable_polls,
        executor_queues=("loops",),
        publish_health=lambda verdict, *, active: (
            health_beats.append((verdict, active)) if health_beats is not None else None
        ),
    )
    worker = LoopWorker(seams)
    return worker, handles


def test_persistent_unreadable_verdict_exits_non_zero() -> None:
    worker, _handles = _worker([FleetAdmission.ADMITS, *([FleetAdmission.UNREADABLE] * 5)], max_unreadable_polls=3)
    with pytest.raises(FleetAdmissionUnreadableError):
        worker.run()


def test_a_preset_admitting_nothing_is_a_clean_stop_not_a_crash() -> None:
    worker, handles = _worker([FleetAdmission.ADMITS, FleetAdmission.NONE])
    worker.run()  # no raise — a posture admitting nothing is the operator's own decision
    assert all(handle.joined for handle in handles)


def test_transient_unreadable_that_recovers_does_not_crash() -> None:
    worker, _ = _worker(
        [FleetAdmission.UNREADABLE, FleetAdmission.UNREADABLE, FleetAdmission.ADMITS, FleetAdmission.NONE],
        max_unreadable_polls=3,
    )
    worker.run()  # the streak reset before hitting the crash threshold


def test_supervisor_publishes_only_completed_active_polls_as_healthy() -> None:
    beats: list[tuple[str, bool]] = []
    worker, _handles = _worker([FleetAdmission.ADMITS, FleetAdmission.NONE], health_beats=beats)
    worker.run()
    assert beats == [("admits", True), ("none", False)]


def test_unreadable_control_publishes_unhealthy_until_a_good_poll() -> None:
    beats: list[tuple[str, bool]] = []
    worker, _handles = _worker(
        [FleetAdmission.ADMITS, FleetAdmission.UNREADABLE, FleetAdmission.ADMITS], health_beats=beats
    )
    worker.run()
    assert beats == [("admits", True), ("unreadable", False), ("admits", True)]
