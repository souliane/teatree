"""Tests for ``teatree.loop.phases.scan`` — the parallel read-then-signal stage."""

from dataclasses import dataclass

from teatree.loop.job_identity import _ScannerJob
from teatree.loop.phases.scan import scan_phase
from teatree.loop.scanners.base import ScanSignal


@dataclass(slots=True)
class _FixedScanner:
    name: str
    out: list[ScanSignal]

    def scan(self) -> list[ScanSignal]:
        return self.out


@dataclass(slots=True)
class _ExplodingScanner:
    name: str = "boom"

    def scan(self) -> list[ScanSignal]:
        msg = "scanner blew up"
        raise RuntimeError(msg)


def test_scan_phase_aggregates_signals_from_every_job() -> None:
    jobs = [
        _ScannerJob(scanner=_FixedScanner(name="a", out=[ScanSignal(kind="my_pr.open", summary="A")]), overlay=""),
        _ScannerJob(scanner=_FixedScanner(name="b", out=[ScanSignal(kind="my_pr.open", summary="B")]), overlay=""),
    ]
    outcome = scan_phase(jobs)
    assert len(outcome.signals) == 2
    assert not outcome.errors


def test_scan_phase_records_scanner_errors_without_raising() -> None:
    jobs = [
        _ScannerJob(scanner=_FixedScanner(name="ok", out=[ScanSignal(kind="my_pr.open", summary="x")]), overlay=""),
        _ScannerJob(scanner=_ExplodingScanner(), overlay=""),
    ]
    outcome = scan_phase(jobs)
    assert len(outcome.signals) == 1
    assert "scanner blew up" in outcome.errors["boom"]


def test_scan_phase_tags_overlay_on_signals() -> None:
    job = _ScannerJob(
        scanner=_FixedScanner(name="s", out=[ScanSignal(kind="my_pr.open", summary="x")]),
        overlay="acme",
    )
    outcome = scan_phase([job])
    assert outcome.signals[0].payload["overlay"] == "acme"


def test_scan_phase_on_empty_jobs_returns_empty_outcome() -> None:
    outcome = scan_phase([])
    assert outcome.signals == []
    assert outcome.errors == {}
