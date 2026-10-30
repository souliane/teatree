import time
from dataclasses import dataclass, field

from teatree.loop.job_identity import _ScannerJob
from teatree.loop.phases.scan import scan_phase
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.scan_deadline import current_scan_deadline, scanner_deadline


@dataclass
class _DeadlineProbe:
    name: str
    seen: list[float | None] = field(default_factory=list)

    def scan(self) -> list[ScanSignal]:
        self.seen.append(current_scan_deadline())
        return []


def test_scan_workers_receive_one_shared_absolute_deadline() -> None:
    scanners = [_DeadlineProbe("first"), _DeadlineProbe("second")]
    started = time.monotonic()
    outside_deadline = started + 30
    with scanner_deadline(outside_deadline):
        outcome = scan_phase([_ScannerJob(scanner=scanner, overlay="") for scanner in scanners], per_job_timeout=5)
        assert current_scan_deadline() == outside_deadline

    assert outcome.errors == {}
    deadlines = [scanner.seen[0] for scanner in scanners]
    assert all(deadline is not None and started < deadline <= time.monotonic() + 5 for deadline in deadlines)
    assert deadlines[0] == deadlines[1]
    assert current_scan_deadline() is None
