"""``teatree.loop.drain`` — quiesce admission, then wait for in-flight to clear.

The drain sets the ``worker_quiescing`` gate ON and polls the in-flight predicate
(a live CLAIMED lease). It returns ``DRAINED`` the moment no lease remains (a quiet
worker returns immediately) and ``GRACE_EXCEEDED`` — naming the still-CLAIMED task
pks — once the timeout lapses, so a deploy can proceed knowing a stuck task
re-queues via its lease lapse. ``sleep`` / ``monotonic`` are injected so the wait is
driven without wall-clock time.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import patch

import django.test
import pytest
from claude_agent_sdk import ClaudeAgentOptions
from django.utils import timezone

from teatree.agents._runner_options import _build_options
from teatree.agents.runner_heartbeat import HeartbeatRuntime, drive_with_heartbeat
from teatree.agents.runner_interruption import CeilingSalvage
from teatree.agents.runner_outcomes import record_outcome
from teatree.agents.runner_usage import DispatchProvenance
from teatree.agents.runner_watchdog import LoopWatchdog, TaskUsage
from teatree.core.managers_task_claim import drain_block_reason
from teatree.core.models import ConfigSetting, WorkerGeneration
from teatree.core.models.task import Task
from teatree.loop.drain import (
    DrainOutcome,
    DrainPacing,
    DrainProgress,
    DrainReport,
    GenerationNotDrainableError,
    QuiescePayload,
    QuiesceStatus,
    drain_worker,
    quiesce_status,
    set_worker_quiescing,
)
from tests.factories import TaskFactory, planned_ticket
from tests.teatree_agents._sdk_fake import InterruptibleSession, OneSessionHarness

_NO_WAIT = DrainPacing(sleep=lambda _seconds: None)


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
        report = drain_worker(timeout=1800, pacing=DrainPacing(sleep=sleeps.append))

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
        report = drain_worker(timeout=30, pacing=DrainPacing(poll_interval=5, sleep=sleeps.append, monotonic=clock))

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

        report = drain_worker(timeout=1800, pacing=DrainPacing(poll_interval=5, sleep=_sleep))

        assert report.outcome is DrainOutcome.DRAINED
        assert sleeps == [5]


class TestDrainProgress(django.test.TestCase):
    """The wait speaks on every poll, so nothing downstream can read it as idle (#3983)."""

    def test_each_poll_reports_the_elapsed_wait_and_the_in_flight_pks(self) -> None:
        task = TaskFactory(status=Task.Status.PENDING)
        task.claim(claimed_by="loop", lease_seconds=300)

        samples: list[DrainProgress] = []
        # monotonic: start=0, two in-budget readings (one progress sample each), then
        # one past the timeout that ends the wait without a third sample.
        clock = _FakeClock([0.0, 10.0, 20.0, 90.0])
        drain_worker(
            timeout=60,
            pacing=DrainPacing(poll_interval=5, sleep=lambda _seconds: None, monotonic=clock),
            on_progress=samples.append,
        )

        assert [round(sample.waited_seconds) for sample in samples] == [10, 20]
        assert all(sample.still_claimed == [task.pk] for sample in samples)

    def test_a_quiet_worker_emits_no_progress(self) -> None:
        samples: list[DrainProgress] = []
        report = drain_worker(timeout=1800, pacing=_NO_WAIT, on_progress=samples.append)

        assert report.outcome is DrainOutcome.DRAINED
        assert samples == []


_N = "a" * 40
_N1 = "b" * 40


def _claimed_under(sha: str) -> Task:
    task = cast("Task", TaskFactory(status=Task.Status.PENDING))
    with patch.dict("os.environ", {"TEATREE_GENERATION": sha}):
        task.claim(claimed_by="loop", lease_seconds=300)
    return task


class TestGenerationScopedDrain(django.test.TestCase):
    def setUp(self) -> None:
        WorkerGeneration.objects.boot(_N)
        WorkerGeneration.objects.boot(_N1)

    def test_a_live_claim_of_the_next_generation_does_not_hold_the_drain(self) -> None:
        _claimed_under(_N1)

        report = drain_worker(timeout=1800, generation=_N, pacing=_NO_WAIT)

        assert report.outcome is DrainOutcome.DRAINED

    def test_the_drain_waits_on_its_own_generations_claims(self) -> None:
        own = _claimed_under(_N)
        _claimed_under(_N1)

        report = drain_worker(
            timeout=30, generation=_N, pacing=DrainPacing(sleep=lambda _s: None, monotonic=_FakeClock([0.0, 50.0]))
        )

        assert report.outcome is DrainOutcome.GRACE_EXCEEDED
        assert report.still_claimed == [own.pk]

    def test_the_generation_is_draining_with_the_grace_as_its_deadline(self) -> None:
        before = timezone.now()

        drain_worker(timeout=600, generation=_N, pacing=_NO_WAIT)

        row = WorkerGeneration.objects.get(sha=_N)
        assert row.state == WorkerGeneration.State.DRAINING
        assert row.drain_deadline is not None
        assert row.drain_deadline >= before + timedelta(seconds=600)

    def test_a_generation_drain_never_writes_the_global_gate(self) -> None:
        drain_worker(timeout=1800, generation=_N, pacing=_NO_WAIT)

        assert not ConfigSetting.objects.filter(key="worker_quiescing").exists()

    def test_re_draining_a_draining_generation_is_idempotent(self) -> None:
        drain_worker(timeout=1800, generation=_N, pacing=_NO_WAIT)

        report = drain_worker(timeout=1800, generation=_N, pacing=_NO_WAIT)

        assert report.drained

    def test_a_drain_that_loses_the_race_to_a_concurrent_one_joins_it(self) -> None:
        begin_drain = WorkerGeneration.begin_drain

        def a_concurrent_drain_lands_first(row: WorkerGeneration, *, deadline: datetime) -> None:
            begin_drain(WorkerGeneration.objects.get(pk=row.pk), deadline=deadline)
            begin_drain(row, deadline=deadline)

        with patch.object(WorkerGeneration, "begin_drain", a_concurrent_drain_lands_first):
            report = drain_worker(timeout=1800, generation=_N, pacing=_NO_WAIT)

        assert report.drained
        assert WorkerGeneration.objects.state_of(_N) == WorkerGeneration.State.DRAINING

    def test_an_unregistered_generation_cannot_be_drained(self) -> None:
        with pytest.raises(GenerationNotDrainableError, match="cccccccccccc is not registered"):
            drain_worker(timeout=1800, generation="c" * 40, pacing=_NO_WAIT)


_RESUMED_SESSION = "0f1e2d3c-4b5a-4968-8776-655443322110"


class TestAnInFlightRunCheckpointsOnTheDrain(django.test.TestCase):
    """The drain ends at the in-flight run's next heartbeat, and the fresh worker resumes it (#5089)."""

    def setUp(self) -> None:
        self.task = cast("Task", TaskFactory(ticket=planned_ticket(), status=Task.Status.PENDING))
        self.task.claim(claimed_by="old-worker", lease_seconds=900)

    def _run_to_its_checkpoint(self, _seconds: float) -> None:
        # Read here: the run's heartbeat thread cannot see this TestCase's uncommitted gate row.
        drain = drain_block_reason()
        harness = OneSessionHarness(InterruptibleSession([], session_id=_RESUMED_SESSION))
        runtime = HeartbeatRuntime(
            watchdog=LoopWatchdog(max_runtime_seconds=5, max_turns=0, max_cost_usd=0.0),
            heartbeat_interval=0.005,
            sample_usage=lambda _task: TaskUsage(turns=0, cost_usd=0.0),
            renew_lease=lambda _task: None,
            drain_reason=lambda: drain,
        )
        outcome = asyncio.run(drive_with_heartbeat(self.task, "p", ClaudeAgentOptions(), harness, runtime=runtime))
        record_outcome(
            self.task, outcome, harness, CeilingSalvage(phase="coding", lane="", provenance=DispatchProvenance())
        )

    def _drain(self) -> DrainReport:
        clock = _FakeClock([0.0, 0.0, 700.0])
        return drain_worker(
            timeout=600, pacing=DrainPacing(poll_interval=0, sleep=self._run_to_its_checkpoint, monotonic=clock)
        )

    def test_the_drain_ends_once_the_run_checkpoints(self) -> None:
        report = self._drain()

        assert report.outcome is DrainOutcome.DRAINED
        assert report.still_claimed == []
        self.task.refresh_from_db()
        assert self.task.status == Task.Status.PENDING, "the run parked to resume; a watchdog kill would read FAILED"

    def test_the_fresh_worker_claims_the_run_and_resumes_its_conversation(self) -> None:
        self._drain()
        set_worker_quiescing(value=False)

        claimed = Task.objects.claim_next_pending(claimed_by="fresh-worker")

        assert claimed == self.task
        assert _build_options(claimed, "ctx", phase="coding", skills=[]).resume == _RESUMED_SESSION


_DRAIN_STARTED = datetime(2026, 10, 6, 16, 26, tzinfo=UTC)


@pytest.mark.parametrize(
    ("status", "line", "payload"),
    [
        pytest.param(
            QuiesceStatus(since=_DRAIN_STARTED, age_seconds=725.4, in_flight=[5461, 5462]),
            "deploy drain: quiescing since 16:26Z (12m), waiting on task(s) 5461, 5462, "
            "which checkpoint at their next heartbeat",
            {"since": "2026-10-06T16:26:00+00:00", "age_seconds": 725, "in_flight": [5461, 5462]},
            id="dated-with-runs-in-flight",
        ),
        pytest.param(
            QuiesceStatus(since=None, age_seconds=None, in_flight=[]),
            "deploy drain: quiescing outside the config store, so undateable, no task in flight",
            {"since": None, "age_seconds": None, "in_flight": []},
            id="undateable-and-idle",
        ),
    ],
)
def test_a_quiesced_worker_reports_its_drain(status: QuiesceStatus, line: str, payload: QuiescePayload) -> None:
    assert status.status_line() == line
    assert status.as_json() == payload


class TestQuiesceStatus(django.test.TestCase):
    def test_an_open_gate_reports_no_drain(self) -> None:
        set_worker_quiescing(value=False)

        assert quiesce_status() is None

    def test_a_quiesced_worker_is_dated_by_its_gate_row(self) -> None:
        set_worker_quiescing(value=True)
        ConfigSetting.objects.filter(key="worker_quiescing").update(updated_at=timezone.now() - timedelta(minutes=3))

        status = quiesce_status()

        assert status is not None
        assert status.since is not None
        assert 170 <= (status.age_seconds or 0) <= 200
