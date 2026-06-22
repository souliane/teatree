"""``scan_phase`` — run the scan jobs in parallel and collect signals.

The read-then-signal stage of a tick: fan the scanner jobs out across a
thread pool, gather every signal, and record each scanner's recoverable
error keyed by its label. No dispatch, no rendering, no DB mutation —
those belong to later phases.
"""

import os
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field

from teatree.loop.domain_jobs import _run_job
from teatree.loop.job_identity import _ScannerJob
from teatree.loop.scanner import serial_scanner_chains
from teatree.loop.scanners.base import ScanSignal

_DEFAULT_PER_JOB_TIMEOUT: float = 60.0
_POOL_WORKERS_PER_CPU: int = 4

_ChainResult = list[tuple[str, list[ScanSignal], str]]


@dataclass(slots=True)
class ScanOutcome:
    signals: list[ScanSignal] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)


def _run_chain(chain: list[_ScannerJob]) -> _ChainResult:
    return [_run_job(job) for job in chain]


def scan_phase(
    jobs: list[_ScannerJob],
    *,
    per_job_timeout: float = _DEFAULT_PER_JOB_TIMEOUT,
) -> ScanOutcome:
    """Fan out scanner jobs across a bounded thread pool.

    Each job is given at most *per_job_timeout* seconds.  A job that exceeds
    its budget is recorded as a timed-out error for that scanner label; the
    tick continues with the remaining results.  The pool is shut down without
    waiting for still-running threads so a hung scanner cannot freeze the tick.

    Dependency-chained scanners (e.g. ``SlackMentionsScanner`` before
    ``SlackReviewIntentScanner``) are grouped into a single serial unit by
    :func:`serial_scanner_chains` and run on one worker, so the depended-upon
    scanner completes before the dependent begins. The chain shares one
    timeout budget; independent scanners still fan out in parallel.
    """
    outcome = ScanOutcome()
    if not jobs:
        return outcome
    chains = serial_scanner_chains(jobs)
    cpu = os.cpu_count() or 4
    max_workers = min(len(chains), cpu * _POOL_WORKERS_PER_CPU)
    pool = ThreadPoolExecutor(max_workers=max(1, max_workers))
    future_to_labels: dict[Future[_ChainResult], list[str]] = {
        pool.submit(_run_chain, chain): [job.scanner.name for job in chain] for chain in chains
    }
    try:
        for future, labels in future_to_labels.items():
            try:
                for job_label, signals, error in future.result(timeout=per_job_timeout):
                    outcome.signals.extend(signals)
                    if error:
                        outcome.errors[job_label] = error
            except TimeoutError:
                for label in labels:
                    outcome.errors[label] = f"scanner timed out after {per_job_timeout}s"
            except Exception as exc:  # noqa: BLE001
                for label in labels:
                    outcome.errors[label] = f"{type(exc).__name__}: {exc}"
    finally:
        # cancel_futures=True drops queued-but-unstarted futures; already-running
        # threads cannot be interrupted but we stop waiting for them so the tick
        # is not frozen by a single hung scanner.
        pool.shutdown(wait=False, cancel_futures=True)
    return outcome
