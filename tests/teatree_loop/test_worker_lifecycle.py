"""``teatree.loop.worker_lifecycle`` — stop the singleton worker, verify it exited.

``drain`` alone closes admission and leaves the supervisor running; on a bare host
nothing then replaces the process, so the box is left admitting ZERO work with the
gate stuck ON. :class:`WorkerStopper` drains, SIGTERMs the flock holder, proves the
exit against the kernel flock probe, and restores ``worker_quiescing`` to its
pre-stop value on EVERY terminal path. :func:`wait_for_new_holder` is the mirror
probe a restart needs: the fresh worker must actually hold the flock.

The process seams (flock probe / recorded pid / signal / clock) are faked — the
suite never signals a real process.
"""

import dataclasses
import signal
import unittest.mock

import django.test
import pytest

from teatree.config.resolution import worker_is_quiescing
from teatree.core.models import ConfigSetting
from teatree.core.models.task import Task
from teatree.loop.drain import QUIESCING_SETTING, DrainOutcome, set_worker_quiescing
from teatree.loop.worker_lifecycle import LifecycleSeams, StopOutcome, StopRequest, WorkerStopper, wait_for_new_holder
from tests.factories import TaskFactory


class _FakeClock:
    """A monotonic/sleep pair where only ``sleep`` advances time."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _FakeWorker:
    """A flock holder driven through the stop seams — it never touches a real process.

    Records the ``worker_quiescing`` value observed at signal time, so a test can prove
    the drain really ran BEFORE the SIGTERM (rather than trusting that it was called).
    """

    def __init__(self, *, pid: int | None = 4242, held: bool = True, exits: bool = True) -> None:
        self.pid = pid
        self.signalled: list[int] = []
        self.quiescing_at_signal: bool | None = None
        self.raise_process_lookup = False
        self._held = held
        self._exits = exits

    def flock_held(self) -> bool:
        return self._held

    def holder_pid(self) -> int | None:
        return self.pid if self._held else None

    def terminate(self, pid: int) -> None:
        self.signalled.append(pid)
        self.quiescing_at_signal = worker_is_quiescing()
        if self._exits:
            self._held = False
        if self.raise_process_lookup:
            raise ProcessLookupError(pid)


def _seams(worker: _FakeWorker, clock: _FakeClock) -> LifecycleSeams:
    return LifecycleSeams(
        flock_held=worker.flock_held,
        holder_pid=worker.holder_pid,
        terminate=worker.terminate,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )


class TestWorkerStopper(django.test.TestCase):
    def test_not_running_when_no_worker_holds_the_flock(self) -> None:
        worker = _FakeWorker(held=False)
        report = WorkerStopper(seams=_seams(worker, _FakeClock())).stop()

        assert report.outcome is StopOutcome.NOT_RUNNING
        assert worker.signalled == []
        # Nothing was drained, so the admission gate was never touched.
        assert ConfigSetting.objects.filter(key=QUIESCING_SETTING).exists() is False

    def test_drains_before_signalling_then_restores_admission(self) -> None:
        worker = _FakeWorker()
        report = WorkerStopper(seams=_seams(worker, _FakeClock())).stop()

        assert report.outcome is StopOutcome.STOPPED
        assert report.holder_pid == 4242
        assert worker.signalled == [4242]
        # The drain really closed admission before the signal ...
        assert worker.quiescing_at_signal is True
        # ... and the stop put it back, so the NEXT worker on this box admits work.
        assert worker_is_quiescing() is False
        assert report.quiescing is False

    def test_no_drain_never_touches_the_admission_gate(self) -> None:
        worker = _FakeWorker()
        report = WorkerStopper(StopRequest(drain=False), _seams(worker, _FakeClock())).stop()

        assert report.outcome is StopOutcome.STOPPED
        assert report.drain is None
        assert worker.quiescing_at_signal is False
        assert ConfigSetting.objects.filter(key=QUIESCING_SETTING).exists() is False

    def test_waits_for_the_flock_release_before_claiming_the_exit(self) -> None:
        worker = _FakeWorker()
        clock = _FakeClock()
        # The worker keeps the flock for two more probes after the signal.
        pending = [2]

        def _flock_held() -> bool:
            if worker.signalled and pending[0] > 0:
                pending[0] -= 1
                return True
            return worker.flock_held()

        seams = dataclasses.replace(_seams(worker, clock), flock_held=_flock_held)
        report = WorkerStopper(seams=seams).stop()

        assert report.outcome is StopOutcome.STOPPED
        assert clock.sleeps  # the exit was polled, not assumed
        assert report.waited_seconds == pytest.approx(sum(clock.sleeps))

    def test_restores_admission_when_the_worker_refuses_to_exit(self) -> None:
        worker = _FakeWorker(exits=False)
        report = WorkerStopper(StopRequest(exit_timeout=2.0), _seams(worker, _FakeClock())).stop()

        assert report.outcome is StopOutcome.STILL_RUNNING
        assert report.holder_pid == 4242
        # The trap: a failed stop must never strand the factory admitting nothing.
        assert worker_is_quiescing() is False
        assert report.quiescing is False

    def test_refuses_to_guess_a_pid_when_none_is_recorded(self) -> None:
        worker = _FakeWorker(pid=None)
        report = WorkerStopper(seams=_seams(worker, _FakeClock())).stop()

        assert report.outcome is StopOutcome.NO_HOLDER_PID
        assert worker.signalled == []
        assert worker_is_quiescing() is False

    def test_reports_a_preexisting_quiesce_instead_of_clearing_it(self) -> None:
        set_worker_quiescing(value=True)
        worker = _FakeWorker()
        report = WorkerStopper(seams=_seams(worker, _FakeClock())).stop()

        assert report.outcome is StopOutcome.STOPPED
        # The operator quiesced on purpose before stopping — the stop restores exactly
        # that state and reports it, so the CLI can name the recovery command.
        assert report.quiescing is True
        assert worker_is_quiescing() is True

    def test_a_holder_that_dies_between_the_pid_read_and_the_signal_counts_as_stopped(self) -> None:
        worker = _FakeWorker()
        worker.raise_process_lookup = True  # the pid was already reaped when the signal landed
        report = WorkerStopper(seams=_seams(worker, _FakeClock())).stop()

        assert report.outcome is StopOutcome.STOPPED
        assert worker.signalled == [4242]

    def test_a_grace_exceeded_drain_still_stops_the_worker(self) -> None:
        task = TaskFactory(status=Task.Status.PENDING)
        task.claim(claimed_by="loop", lease_seconds=300)  # a live lease that never clears

        worker = _FakeWorker()
        report = WorkerStopper(
            StopRequest(drain_timeout=30, drain_poll_seconds=5.0),
            _seams(worker, _FakeClock()),
        ).stop()

        assert report.drain is not None
        assert report.drain.outcome is DrainOutcome.GRACE_EXCEEDED
        assert report.drain.still_claimed == [task.pk]
        assert report.outcome is StopOutcome.STOPPED
        assert worker_is_quiescing() is False


class TestWaitForNewHolder(django.test.TestCase):
    def test_a_different_pid_on_the_flock_proves_the_restart(self) -> None:
        worker = _FakeWorker(pid=999)
        report = wait_for_new_holder(previous_pid=4242, seams=_seams(worker, _FakeClock()))

        assert report.started is True
        assert report.holder_pid == 999

    def test_the_old_pid_still_holding_is_never_a_successful_restart(self) -> None:
        worker = _FakeWorker(pid=4242)
        clock = _FakeClock()
        report = wait_for_new_holder(previous_pid=4242, timeout=3.0, seams=_seams(worker, clock))

        assert report.started is False
        assert report.waited_seconds >= 3.0
        assert clock.sleeps  # it waited, it did not fail fast on the first probe

    def test_a_free_flock_is_never_a_successful_restart(self) -> None:
        worker = _FakeWorker(held=False)
        report = wait_for_new_holder(previous_pid=None, timeout=2.0, seams=_seams(worker, _FakeClock()))

        assert report.started is False
        assert report.holder_pid is None

    def test_a_held_flock_with_no_recorded_pid_counts_as_started(self) -> None:
        # The flock is authoritative (a pid file can be missing/stale while a worker
        # holds the lock) — the same rule `t3 worker status` follows.
        worker = _FakeWorker(pid=None)
        report = wait_for_new_holder(previous_pid=4242, seams=_seams(worker, _FakeClock()))

        assert report.started is True
        assert report.holder_pid is None


class TestDefaultProcessSeams:
    """The production LifecycleSeams defaults wire the real flock/pid/signal primitives."""

    def test_flock_held_delegates_to_the_singleton_probe(self) -> None:
        with unittest.mock.patch("teatree.loop.worker_lifecycle.flock_is_held", return_value=True) as flock:
            assert LifecycleSeams().flock_held() is True
        flock.assert_called_once()

    def test_holder_pid_reads_the_singleton_pid_file(self) -> None:
        with unittest.mock.patch("teatree.loop.worker_lifecycle.read_pid", return_value=4242) as read:
            assert LifecycleSeams().holder_pid() == 4242
        read.assert_called_once()

    def test_terminate_sends_sigterm_to_the_pid(self) -> None:
        with unittest.mock.patch("teatree.loop.worker_lifecycle.os.kill") as kill:
            LifecycleSeams().terminate(4242)
        kill.assert_called_once_with(4242, signal.SIGTERM)
