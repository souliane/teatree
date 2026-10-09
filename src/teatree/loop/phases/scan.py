"""``scan_phase`` — run the scan jobs in parallel and collect signals.

The read-then-signal stage of a tick: fan the scanner jobs out across a
thread pool, gather every signal, and record each scanner's recoverable
error keyed by its label. No dispatch, no rendering, no DB mutation —
those belong to later phases.
"""

import logging
import os
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from queue import Empty, SimpleQueue
from threading import Thread

from teatree.loop.domain_jobs import _run_job
from teatree.loop.job_identity import _ScannerJob
from teatree.loop.scanners.base import ScanSignal
from teatree.utils.thread_db import close_thread_db_connections

SCAN_DEADLINE_SECONDS: float = 60.0
_POOL_WORKERS_PER_CPU: int = 4


@dataclass(slots=True)
class ScanOutcome:
    signals: list[ScanSignal] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)


type _ScanResult = tuple[str, list[ScanSignal], str]
type _ScanWork = tuple[Future[_ScanResult], Callable[[_ScannerJob], _ScanResult], _ScannerJob]
logger = logging.getLogger(__name__)


class _AbandonableScanPool:
    """Run live scanners on daemon workers so an overrun cannot pin interpreter exit."""

    def __init__(self, *, max_workers: int) -> None:
        self._jobs: SimpleQueue[_ScanWork | None] = SimpleQueue()
        self._futures: list[Future[_ScanResult]] = []
        self._closed = False
        self._threads = [Thread(target=self._work, name=f"scan-worker-{i}", daemon=True) for i in range(max_workers)]
        for thread in self._threads:
            thread.start()

    def submit(self, fn: Callable[[_ScannerJob], _ScanResult], job: _ScannerJob) -> Future[_ScanResult]:
        if self._closed:
            msg = "scan pool is shut down"
            raise RuntimeError(msg)
        future: Future[_ScanResult] = Future()
        self._futures.append(future)
        self._jobs.put((future, fn, job))
        return future

    def _work(self) -> None:
        while (item := self._jobs.get()) is not None:
            future, fn, job = item
            if not future.set_running_or_notify_cancel():
                continue
            try:
                result = fn(job)
            except BaseException as exc:
                logger.exception("Scan worker failed")
                future.set_exception(exc)
            else:
                future.set_result(result)

    def shutdown(self) -> None:
        self._closed = True
        while True:
            try:
                item = self._jobs.get_nowait()
            except Empty:
                break
            if item is not None:
                item[0].cancel()
        for _ in self._threads:
            self._jobs.put(None)
        if all(future.done() for future in self._futures):
            for thread in self._threads:
                thread.join()


def _run_job_closing_connections(job: _ScannerJob) -> _ScanResult:
    """Run one scan job on a pool worker, then close that worker's DB connections.

    A scanner that touches the ORM opens a thread-local Django connection on its
    pool worker. Nothing else closes it: a worker that outlives the deadline is never
    joined, so the orphaned handle is finalized at an arbitrary later GC — see
    :mod:`teatree.utils.thread_db` for why that surfaces as a red in an unrelated
    test. A no-op for a scanner that never opened a connection.
    """
    try:
        return _run_job(job)
    finally:
        close_thread_db_connections()


def scan_pool_size(job_count: int) -> int:
    return max(1, min(job_count, (os.cpu_count() or 4) * _POOL_WORKERS_PER_CPU))


def collect_scan(
    pool: ThreadPoolExecutor | _AbandonableScanPool,
    jobs: list[_ScannerJob],
    *,
    per_job_timeout: float,
    overrun_note: str,
) -> ScanOutcome:
    """Submit *jobs* to *pool* and gather their results under ONE shared absolute deadline.

    The caller owns *pool* and its shutdown, which is where the live tick and the followup
    preview differ: live abandons a scanner past the deadline, the preview joins it.
    """
    outcome = ScanOutcome()
    future_to_label: dict[Future[_ScanResult], str] = {
        pool.submit(_run_job_closing_connections, job): job.scanner.name for job in jobs
    }
    deadline = time.monotonic() + per_job_timeout
    for future, label in future_to_label.items():
        try:
            remaining = max(0.0, deadline - time.monotonic())
            job_label, signals, error = future.result(timeout=remaining)
            outcome.signals.extend(signals)
            if error:
                outcome.errors[job_label] = error
        except TimeoutError:
            outcome.errors[label] = f"scanner timed out after {per_job_timeout}s ({overrun_note})"
        except Exception as exc:  # noqa: BLE001 — a scanner failure is recorded per-label, never aborts the scan phase
            outcome.errors[label] = f"{type(exc).__name__}: {exc}"
    return outcome


def scan_phase(
    jobs: list[_ScannerJob],
    *,
    per_job_timeout: float = SCAN_DEADLINE_SECONDS,
) -> ScanOutcome:
    """Fan out scanner jobs across a bounded thread pool under ONE shared deadline.

    The deadline is shared by all jobs. A timed-out scanner remains active in a daemon
    worker until it finishes or the process exits; it cannot hold interpreter shutdown.
    A slow scanner may run beside its next-tick instance and mutate state after its tick ends.
    """
    if not jobs:
        return ScanOutcome()
    pool = _AbandonableScanPool(max_workers=scan_pool_size(len(jobs)))
    try:
        return collect_scan(pool, jobs, per_job_timeout=per_job_timeout, overrun_note="abandoned, still running")
    finally:
        pool.shutdown()
