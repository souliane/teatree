"""Tests for ``teatree.loop.phases.scan`` — the parallel read-then-signal stage."""

import sqlite3
import time
from dataclasses import dataclass, field

import pytest
from django.test import TestCase

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


@dataclass(slots=True)
class _HungScanner:
    name: str = "hung"

    def scan(self) -> list[ScanSignal]:
        # Sleep far longer than the test timeout; the pool must interrupt it.
        time.sleep(60)
        return []  # pragma: no cover — never reached under timeout


def test_scan_phase_times_out_hung_scanner_and_records_error() -> None:
    """A hung scanner is interrupted after per_job_timeout and its error is recorded (fix #4)."""
    jobs = [
        _ScannerJob(scanner=_HungScanner(), overlay=""),
        _ScannerJob(scanner=_FixedScanner(name="ok", out=[ScanSignal(kind="my_pr.open", summary="x")]), overlay=""),
    ]
    outcome = scan_phase(jobs, per_job_timeout=0.1)
    # The ok scanner's signal is present
    assert any(s.summary == "x" for s in outcome.signals)
    # The hung scanner's error is recorded
    assert "hung" in outcome.errors
    assert "timeout" in outcome.errors["hung"].lower() or "timed" in outcome.errors["hung"].lower()


def test_timed_out_scanner_error_is_labelled_abandoned() -> None:
    """F5.9: a timed-out scanner thread is abandoned (kept running), not cancelled.

    The recorded error must make the still-running state explicit so the tick
    report / statusline does not imply the scanner simply stopped — a later tick
    may start a second instance of it.
    """
    jobs = [_ScannerJob(scanner=_HungScanner(), overlay="")]
    outcome = scan_phase(jobs, per_job_timeout=0.1)
    assert "abandoned" in outcome.errors["hung"].lower()
    assert "still running" in outcome.errors["hung"].lower()


def test_scan_phase_bounds_all_jobs_under_one_shared_deadline() -> None:
    """Two hung scanners share ONE absolute deadline — never N x per_job_timeout (fix #7)."""
    jobs = [
        _ScannerJob(scanner=_HungScanner(name="h1"), overlay=""),
        _ScannerJob(scanner=_HungScanner(name="h2"), overlay=""),
    ]
    start = time.monotonic()
    outcome = scan_phase(jobs, per_job_timeout=0.3)
    elapsed = time.monotonic() - start

    assert "h1" in outcome.errors
    assert "h2" in outcome.errors
    # One shared deadline: well under the ~0.6s a per-job sequential wait would charge.
    assert elapsed < 0.5


@dataclass(slots=True)
class _OrmTouchingScanner:
    """A scanner that opens its pool worker's thread-local Django connection.

    Captures the raw DB-API connection it opened so a test can assert the phase
    closed it.
    """

    name: str = "orm-touch"
    raw_connections: list[sqlite3.Connection] = field(default_factory=list)

    def scan(self) -> list[ScanSignal]:
        from django.db import connection  # noqa: PLC0415

        connection.ensure_connection()
        self.raw_connections.append(connection.connection)
        return []


class TestScanPhaseConnectionHygiene(TestCase):
    """A pool worker that touches the ORM must not leak its DB connection.

    The leak only manifests under a Django ``TestCase``: the pool thread is not
    the test's transaction-owning thread, so an unclosed thread-local Django
    connection is finalized at GC as a ``sqlite3`` ``ResourceWarning`` that
    surfaces as an unraisable-exception error in an unrelated later test (the
    same per-thread hygiene ``teatree.loops.worker`` applies).
    """

    def test_scan_phase_closes_a_worker_threads_db_connection(self) -> None:
        scanner = _OrmTouchingScanner()
        scan_phase([_ScannerJob(scanner=scanner, overlay="")])

        assert scanner.raw_connections, "the scanner never opened a connection"
        raw = scanner.raw_connections[0]
        # A closed sqlite3 connection raises when used; an open (leaked) one does not.
        with pytest.raises(sqlite3.ProgrammingError):
            raw.execute("SELECT 1")


def test_scan_phase_worker_pool_is_bounded() -> None:
    """Pool size is capped even when many jobs are present."""
    import os  # noqa: PLC0415
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415
    from unittest.mock import patch  # noqa: PLC0415

    max_seen: list[int] = []

    def _capped_tpe(*, max_workers: int | None = None, **kwargs: object) -> ThreadPoolExecutor:
        max_seen.append(max_workers or 0)
        return ThreadPoolExecutor(max_workers=max_workers, **kwargs)  # type: ignore[arg-type]

    jobs = [_ScannerJob(scanner=_FixedScanner(name=f"s{i}", out=[]), overlay="") for i in range(200)]
    cpu = os.cpu_count() or 4
    expected_cap = min(200, cpu * 4)

    with patch("teatree.loop.phases.scan.ThreadPoolExecutor", side_effect=_capped_tpe):
        scan_phase(jobs)

    assert max_seen, "ThreadPoolExecutor was not called"
    assert max_seen[0] <= expected_cap
