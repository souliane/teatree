"""``teatree.loop.drain`` — quiesce admission, then wait for in-flight to clear.

The drain sets the ``worker_quiescing`` gate ON and polls the in-flight predicate
(a live CLAIMED lease). It returns ``DRAINED`` the moment no lease remains (a quiet
worker returns immediately) and ``GRACE_EXCEEDED`` — naming the still-CLAIMED task
pks — once the timeout lapses, so a deploy can proceed knowing a stuck task
re-queues via its lease lapse. ``sleep`` / ``monotonic`` are injected so the wait is
driven without wall-clock time.
"""

import django.test
import pytest

from teatree.core.models import ConfigSetting
from teatree.core.models.task import Task
from teatree.loop.drain import DrainOutcome, drain_worker, set_worker_quiescing
from tests.factories import TaskFactory


class _FakeClock:
    """A monotonic() that emits a scripted sequence of elapsed readings."""

    def __init__(self, readings: list[float]) -> None:
        self._readings = readings
        self._i = 0

    def __call__(self) -> float:
        value = self._readings[min(self._i, len(self._readings) - 1)]
        self._i += 1
        return value


class TestSetWorkerQuiescing(django.test.TestCase):
    def test_writes_the_gate_to_the_config_store(self) -> None:
        set_worker_quiescing(value=True)
        assert ConfigSetting.objects.get_effective("worker_quiescing") is True

        set_worker_quiescing(value=False)
        assert ConfigSetting.objects.get_effective("worker_quiescing") is False


class TestDrainWorker(django.test.TestCase):
    def test_drains_immediately_when_nothing_in_flight(self) -> None:
        sleeps: list[float] = []
        report = drain_worker(timeout=1800, sleep=sleeps.append)

        assert report.outcome is DrainOutcome.DRAINED
        assert report.still_claimed == []
        # No wait was needed — the very first in-flight check was clear.
        assert sleeps == []
        # The gate is left ON — the deploy swap follows, and the fresh worker clears it.
        assert ConfigSetting.objects.get_effective("worker_quiescing") is True

    def test_grace_exceeded_lists_the_still_claimed_tasks(self) -> None:
        task = TaskFactory(status=Task.Status.PENDING)
        task.claim(claimed_by="loop", lease_seconds=300)  # a live CLAIMED lease that never clears

        sleeps: list[float] = []
        # monotonic: start=0, then an elapsed reading past the timeout on the first
        # in-flight loop — so the wait ends GRACE_EXCEEDED without ever sleeping.
        clock = _FakeClock([0.0, 50.0])
        report = drain_worker(timeout=30, poll_interval=5, sleep=sleeps.append, monotonic=clock)

        assert report.outcome is DrainOutcome.GRACE_EXCEEDED
        assert report.drained is False
        assert report.still_claimed == [task.pk]
        assert report.waited_seconds == pytest.approx(50.0)
        assert sleeps == []

    def test_polls_until_the_lease_clears(self) -> None:
        task = TaskFactory(status=Task.Status.PENDING)
        task.claim(claimed_by="loop", lease_seconds=300)

        # The in-flight task finishes (goes terminal) on the first poll: the initial
        # check sees it CLAIMED (sleep once), the sleep clears the lease, and the next
        # check returns DRAINED.
        sleeps: list[float] = []

        def _sleep(seconds: float) -> None:
            sleeps.append(seconds)
            Task.objects.filter(pk=task.pk).update(status=Task.Status.COMPLETED)

        report = drain_worker(timeout=1800, poll_interval=5, sleep=_sleep)

        assert report.outcome is DrainOutcome.DRAINED
        assert sleeps == [5]
