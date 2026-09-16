"""The worker crashes NON-ZERO on a persistent kill-switch READ FAILURE (F7).

A read that RAISES is UNREADABLE, not OFF. Before the fix the read collapsed to False
and the worker clean-exited 0, so ``restart: on-failure`` never restarted a worker
downed by a transient DB error — the factory sat silently dead. Now UNREADABLE is
retried a few polls (a blip recovers) then crashes; a legitimate OFF idles cleanly.
"""

import pytest

from teatree.loops.timer_chains import LoopRunnerState
from teatree.loops.worker import KillSwitchUnreadableError, LoopWorker, WorkerSeams


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
        return True

    def join(self, timeout: float | None = None) -> None:
        self.joined = True


def _worker(states: list[LoopRunnerState], *, max_unreadable_polls: int = 3):
    handles: list[_FakeHandle] = []
    it = iter(states)
    worker = None

    def read_state() -> LoopRunnerState:
        try:
            return next(it)
        except StopIteration:
            worker.request_stop()
            return LoopRunnerState.OFF

    def spawn(_executor: object) -> _FakeHandle:
        handle = _FakeHandle()
        handles.append(handle)
        return handle

    seams = WorkerSeams(
        read_state=read_state,
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
    )
    worker = LoopWorker(seams)
    return worker, handles


def test_persistent_unreadable_kill_switch_exits_non_zero() -> None:
    worker, handles = _worker([LoopRunnerState.UNREADABLE] * 5, max_unreadable_polls=3)
    with pytest.raises(KillSwitchUnreadableError):
        worker.run()
    assert all(handle.joined for handle in handles)  # the pool is still torn down on the crash path


def test_legitimate_off_is_clean_idle_not_a_crash() -> None:
    worker, handles = _worker([LoopRunnerState.OFF])
    worker.run()  # no raise
    assert all(handle.joined for handle in handles)


def test_transient_unreadable_that_recovers_does_not_crash() -> None:
    # Two unreadable polls (a blip), then a good read resets the streak, then clean idle.
    worker, _ = _worker(
        [LoopRunnerState.UNREADABLE, LoopRunnerState.UNREADABLE, LoopRunnerState.ON, LoopRunnerState.OFF],
        max_unreadable_polls=3,
    )
    worker.run()  # no raise — the streak reset before hitting the crash threshold
