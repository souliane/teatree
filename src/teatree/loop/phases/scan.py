"""``scan_phase`` — run the scan jobs in parallel and collect signals.

The read-then-signal stage of a tick: fan the scanner jobs out across a
thread pool, gather every signal, and record each scanner's recoverable
error keyed by its label. No dispatch, no rendering, no DB mutation —
those belong to later phases.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from teatree.loop.domain_jobs import _run_job
from teatree.loop.job_identity import _ScannerJob
from teatree.loop.scanners.base import ScanSignal


@dataclass(slots=True)
class ScanOutcome:
    signals: list[ScanSignal] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)


def scan_phase(jobs: list[_ScannerJob]) -> ScanOutcome:
    outcome = ScanOutcome()
    if not jobs:
        return outcome
    with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as pool:
        for label, signals, error in pool.map(_run_job, jobs):
            outcome.signals.extend(signals)
            if error:
                outcome.errors[label] = error
    return outcome
