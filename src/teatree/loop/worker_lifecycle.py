"""Stop / restart control for the singleton worker — the bare-host half of drain-then-deploy.

:mod:`teatree.loop.drain` closes admission and waits for in-flight leases; it never stops
the supervisor, because a container deploy replaces the process and ``deploy/entrypoint.sh``
clears ``worker_quiescing`` on the fresh boot. On a bare host nothing replaces it: after a
drain the worker keeps running with the gate ON, so the box admits ZERO work with no
indication and no discoverable way back.

:class:`WorkerStopper` closes that gap. It drains (REUSING ``drain_worker``), signals the
flock holder, and proves the exit against the kernel ``flock`` probe — the same
authoritative mechanism ``t3 worker status`` reads, never a scan for a plausible-looking
pid. The pid it signals comes from the singleton's own pid file, which the holder writes
UNDER the lock, so while the flock is held the recorded pid is the holder's; a missing or
dead pid is reported (:attr:`StopOutcome.NO_HOLDER_PID`), never guessed around.

The admission gate is restored to its exact pre-stop value on EVERY terminal path — a stop
that fails must never strand the factory quiesced, and a stop that succeeds must not poison
the next worker the operator starts. The final effective value is read BACK from the store
into :attr:`StopReport.quiescing`, so the caller reports what is actually true rather than
what was intended.

:func:`wait_for_new_holder` is the mirror probe a restart needs: ``spawn_detached_worker``
reports success as soon as the ``t3`` binary exists (the child's streams go to ``DEVNULL``),
so a startup crash is invisible in its return value — only a flock held by a DIFFERENT pid
proves a fresh worker is up.
"""

import contextlib
import os
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from teatree.config.resolution import worker_is_quiescing
from teatree.loop.drain import DrainReport, drain_worker, set_worker_quiescing
from teatree.utils.singleton import WORKER_SINGLETON, default_pid_path, flock_is_held, read_pid

#: The default drain grace, mirroring ``t3 worker drain --timeout``.
DEFAULT_DRAIN_TIMEOUT_SECONDS = 1800
#: How long a SIGTERMed worker gets to release the flock. Its supervisor re-reads the
#: stop event every ~5 s and then joins the executors (~3 s), so a healthy shutdown lands
#: well inside this window — exceeding it means the exit did NOT happen.
DEFAULT_EXIT_TIMEOUT_SECONDS = 60.0
#: How long a freshly spawned worker gets to acquire the flock (Django bootstrap + the
#: startup reconcile run before the singleton is taken).
DEFAULT_START_TIMEOUT_SECONDS = 60.0
_EXIT_POLL_SECONDS = 0.5
_START_POLL_SECONDS = 1.0


class StopOutcome(Enum):
    """Terminal state of a stop attempt — every non-``STOPPED`` value is a failure to report."""

    STOPPED = "stopped"
    NOT_RUNNING = "not_running"
    NO_HOLDER_PID = "no_holder_pid"
    STILL_RUNNING = "still_running"


def _worker_flock_held() -> bool:
    return flock_is_held(WORKER_SINGLETON, pid_path=default_pid_path(WORKER_SINGLETON))


def _worker_holder_pid() -> int | None:
    return read_pid(default_pid_path(WORKER_SINGLETON))


def _terminate(pid: int) -> None:
    os.kill(pid, signal.SIGTERM)


@dataclass(frozen=True)
class LifecycleSeams:
    """The process seams — the production defaults wire the real flock probe, pid file, and signal.

    Injectable so the stop/restart logic is exercised without a real process, a real
    signal, or wall-clock time.
    """

    flock_held: Callable[[], bool] = _worker_flock_held
    holder_pid: Callable[[], int | None] = _worker_holder_pid
    terminate: Callable[[int], None] = _terminate
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic


@dataclass(frozen=True, slots=True)
class StopRequest:
    """What the operator asked for: drain first (default), and how long to wait for each phase."""

    drain: bool = True
    drain_timeout: int = DEFAULT_DRAIN_TIMEOUT_SECONDS
    drain_poll_seconds: float = 5.0
    exit_timeout: float = DEFAULT_EXIT_TIMEOUT_SECONDS


@dataclass(frozen=True, slots=True)
class StopReport:
    outcome: StopOutcome
    holder_pid: int | None = None
    #: The drain that ran first, or ``None`` under ``--no-drain``.
    drain: DrainReport | None = None
    #: Seconds spent waiting for the flock to be released after the signal.
    waited_seconds: float = 0.0
    #: The effective ``worker_quiescing`` READ BACK after the attempt — ``True`` means the
    #: box still admits no work and the operator must be told, in plain words, how to clear it.
    quiescing: bool = False

    @property
    def worker_gone(self) -> bool:
        """No worker holds the flock now — either this stop ended it or none was running."""
        return self.outcome in {StopOutcome.STOPPED, StopOutcome.NOT_RUNNING}


@dataclass(frozen=True, slots=True)
class StartReport:
    #: ``True`` only when the flock is held by something other than the stopped pid.
    started: bool
    holder_pid: int | None
    waited_seconds: float


def _restore_admission(*, quiescing_before: bool) -> None:
    """Put ``worker_quiescing`` back exactly as the stop found it.

    The trap this closes: a drain turns admission OFF, and on a bare host nothing turns
    it back on (only a container boot's ``deploy/entrypoint.sh`` does). A stop that then
    fails would leave the factory admitting nothing with no indication; a stop that
    succeeds would hand the next worker a closed gate.
    """
    if worker_is_quiescing() != quiescing_before:
        set_worker_quiescing(value=quiescing_before)


class WorkerStopper:
    """Drain, signal the flock holder, verify the exit, and never strand the quiesce gate."""

    def __init__(self, request: StopRequest | None = None, seams: LifecycleSeams | None = None) -> None:
        self._request = request or StopRequest()
        self._seams = seams or LifecycleSeams()

    def stop(self) -> StopReport:
        quiescing_before = worker_is_quiescing()
        if not self._seams.flock_held():
            return StopReport(outcome=StopOutcome.NOT_RUNNING, quiescing=quiescing_before)

        drain_report = self._drain() if self._request.drain else None
        outcome, pid, waited = self._terminate_and_verify()
        _restore_admission(quiescing_before=quiescing_before)
        return StopReport(
            outcome=outcome,
            holder_pid=pid,
            drain=drain_report,
            waited_seconds=waited,
            quiescing=worker_is_quiescing(),
        )

    def _drain(self) -> DrainReport:
        return drain_worker(
            timeout=self._request.drain_timeout,
            poll_interval=self._request.drain_poll_seconds,
            sleep=self._seams.sleep,
            monotonic=self._seams.monotonic,
        )

    def _terminate_and_verify(self) -> tuple[StopOutcome, int | None, float]:
        """SIGTERM the recorded holder, then let the FLOCK decide whether it really exited."""
        pid = self._seams.holder_pid()
        if pid is None:
            return StopOutcome.NO_HOLDER_PID, None, 0.0
        # A holder that died between the pid read and the signal is not an error — the
        # flock probe below is what decides whether a worker is still there.
        with contextlib.suppress(ProcessLookupError):
            self._seams.terminate(pid)
        released, waited = self._await_flock_release()
        return (StopOutcome.STOPPED if released else StopOutcome.STILL_RUNNING), pid, waited

    def _await_flock_release(self) -> tuple[bool, float]:
        start = self._seams.monotonic()
        while True:
            if not self._seams.flock_held():
                return True, self._seams.monotonic() - start
            waited = self._seams.monotonic() - start
            if waited >= self._request.exit_timeout:
                return False, waited
            self._seams.sleep(_EXIT_POLL_SECONDS)


def wait_for_new_holder(
    *,
    previous_pid: int | None,
    timeout: float = DEFAULT_START_TIMEOUT_SECONDS,
    seams: LifecycleSeams | None = None,
) -> StartReport:
    """Poll the kernel flock until a worker OTHER than ``previous_pid`` holds it.

    The independent check a restart needs — the spawner's own verdict proves only that a
    ``t3`` binary was launched. A held flock whose recorded pid is absent still counts as
    a new holder (``read_pid`` returns ``None`` for a dead/missing pid, and the flock is
    authoritative — the rule ``t3 worker status`` follows); the stopped pid holding it
    again never does.
    """
    live = seams or LifecycleSeams()
    start = live.monotonic()
    while True:
        if live.flock_held():
            pid = live.holder_pid()
            if pid is None or pid != previous_pid:
                return StartReport(started=True, holder_pid=pid, waited_seconds=live.monotonic() - start)
        waited = live.monotonic() - start
        if waited >= timeout:
            return StartReport(started=False, holder_pid=live.holder_pid(), waited_seconds=waited)
        live.sleep(_START_POLL_SECONDS)


__all__ = [
    "DEFAULT_DRAIN_TIMEOUT_SECONDS",
    "DEFAULT_EXIT_TIMEOUT_SECONDS",
    "DEFAULT_START_TIMEOUT_SECONDS",
    "LifecycleSeams",
    "StartReport",
    "StopOutcome",
    "StopReport",
    "StopRequest",
    "WorkerStopper",
    "wait_for_new_holder",
]
