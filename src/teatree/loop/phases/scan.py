"""``scan_phase`` — run the scan jobs in parallel and collect signals.

The read-then-signal stage of a tick: fan the scanner jobs out across a
thread pool, gather every signal, and record each scanner's recoverable
error keyed by its label. No dispatch, no rendering, no DB mutation —
those belong to later phases.
"""

import os
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field

from teatree.loop.domain_jobs import _run_job
from teatree.loop.job_identity import _ScannerJob
from teatree.loop.scanners.base import ScanSignal
from teatree.utils.thread_db import close_thread_db_connections

_DEFAULT_PER_JOB_TIMEOUT: float = 60.0
_POOL_WORKERS_PER_CPU: int = 4


@dataclass(slots=True)
class ScanOutcome:
    signals: list[ScanSignal] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)


def _run_job_closing_connections(job: _ScannerJob) -> tuple[str, list[ScanSignal], str]:
    """Run one scan job on a pool worker, then close that worker's DB connections.

    A scanner that touches the ORM opens a thread-local Django connection on its
    pool worker. Nothing else closes it: ``pool.shutdown(wait=False)`` never joins
    the worker, so the orphaned handle is finalized at an arbitrary later GC — see
    :mod:`teatree.utils.thread_db` for why that surfaces as a red in an unrelated
    test. A no-op for a scanner that never opened a connection.
    """
    try:
        return _run_job(job)
    finally:
        close_thread_db_connections()


def scan_phase(
    jobs: list[_ScannerJob],
    *,
    per_job_timeout: float = _DEFAULT_PER_JOB_TIMEOUT,
) -> ScanOutcome:
    """Fan out scanner jobs across a bounded thread pool under ONE shared deadline.

    The whole scan phase is bounded by a single *per_job_timeout*-second ABSOLUTE
    deadline, not a fresh per-job timeout applied sequentially: waiting on each future
    with its own full timeout charged N x *per_job_timeout* in the worst case, so a few
    hung scanners could pin the tick subprocess (and one of the worker's scarce pinned
    executor slots) for minutes. Each future is now waited on for only the time REMAINING
    until the shared deadline; a job past it is recorded as a timed-out error for that
    scanner label and the tick continues.  The pool is shut down without waiting for
    still-running threads so a hung scanner cannot freeze the tick.

    Abandoned-thread behaviour (F5.9): ``pool.shutdown(wait=False, cancel_futures=True)``
    drops only the futures that have not STARTED running. A scanner thread already
    executing past the deadline cannot be interrupted — Python has no thread-kill — so it
    is ABANDONED: it keeps running to completion in the background while the tick moves on.
    Two consequences the caller must be aware of, since there is (deliberately, for now) no
    in-flight guard: (a) the abandoned thread may still mutate shared state (DB rows, forge
    reads) after the tick that spawned it has ended, and (b) the NEXT tick will schedule the
    same scanner again, so a chronically-slow scanner can have two instances of itself
    running concurrently. The timed-out error is labelled ``(abandoned, still running)`` so
    the tick report / statusline surfaces the still-running state rather than implying the
    scanner simply stopped.
    """
    outcome = ScanOutcome()
    if not jobs:
        return outcome
    cpu = os.cpu_count() or 4
    max_workers = min(len(jobs), cpu * _POOL_WORKERS_PER_CPU)
    pool = ThreadPoolExecutor(max_workers=max(1, max_workers))
    future_to_label: dict[Future[tuple[str, list[ScanSignal], str]], str] = {
        pool.submit(_run_job_closing_connections, job): job.scanner.name for job in jobs
    }
    deadline = time.monotonic() + per_job_timeout
    try:
        for future, label in future_to_label.items():
            try:
                remaining = max(0.0, deadline - time.monotonic())
                job_label, signals, error = future.result(timeout=remaining)
                outcome.signals.extend(signals)
                if error:
                    outcome.errors[job_label] = error
            except TimeoutError:
                # F5.9: the thread is not cancelled — it is abandoned and keeps
                # running in the background. Label it so the error output makes the
                # still-running state explicit (a next tick may start a 2nd instance).
                outcome.errors[label] = f"scanner timed out after {per_job_timeout}s (abandoned, still running)"
            except Exception as exc:  # noqa: BLE001 — a scanner failure is recorded per-label, never aborts the scan phase
                outcome.errors[label] = f"{type(exc).__name__}: {exc}"
    finally:
        # cancel_futures=True drops queued-but-unstarted futures; already-running
        # threads cannot be interrupted but we stop waiting for them so the tick
        # is not frozen by a single hung scanner.
        pool.shutdown(wait=False, cancel_futures=True)
    return outcome
