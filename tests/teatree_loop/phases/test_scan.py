"""Tests for ``teatree.loop.phases.scan`` — the parallel read-then-signal stage."""

import os
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.loop.job_identity import _ScannerJob
from teatree.loop.phases.scan import _AbandonableScanPool, _run_job_closing_connections, scan_phase
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


def test_raising_scanner_before_a_healthy_job_does_not_abort_the_tick() -> None:
    jobs = [
        _ScannerJob(scanner=_ExplodingScanner(name="first"), overlay="acme"),
        _ScannerJob(
            scanner=_FixedScanner(name="second", out=[ScanSignal(kind="my_pr.open", summary="kept")]), overlay="acme"
        ),
    ]

    outcome = scan_phase(jobs)

    assert [(signal.summary, signal.payload["overlay"]) for signal in outcome.signals] == [("kept", "acme")]
    assert outcome.errors == {"first[acme]": "RuntimeError: scanner blew up"}


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


_HUNG_RELEASE = threading.Event()
_HUNG_THREADS: list[threading.Thread] = []


@pytest.fixture(autouse=True)
def _join_hung_scanners() -> Iterator[None]:
    _HUNG_RELEASE.clear()
    _HUNG_THREADS.clear()
    yield
    _HUNG_RELEASE.set()
    for worker in _HUNG_THREADS:
        worker.join(timeout=5)
        assert not worker.is_alive(), f"scanner thread {worker.name} did not stop"


@dataclass(slots=True)
class _HungScanner:
    name: str = "hung"

    def scan(self) -> list[ScanSignal]:
        _HUNG_THREADS.append(threading.current_thread())
        _HUNG_RELEASE.wait()
        return []


def test_scan_phase_times_out_hung_scanner_and_records_error() -> None:
    """A hung scanner is reported after the deadline and released at teardown."""
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


def test_timed_out_scanner_does_not_hold_interpreter_open() -> None:
    script = """
import os
import threading
import django

os.environ["DJANGO_SETTINGS_MODULE"] = "tests.django_settings"
django.setup()
from teatree.loop.job_identity import _ScannerJob
from teatree.loop.phases.scan import scan_phase

class HeldScanner:
    name = "held"

    def scan(self):
        threading.Event().wait()

scan_phase([_ScannerJob(scanner=HeldScanner(), overlay="")], per_job_timeout=0.01)
print("scan returned", flush=True)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=Path(__file__).resolve().parents[3],
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "scan returned"


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


class _ScanAbortedError(BaseException):
    pass


def _abort(_job: _ScannerJob) -> NoReturn:
    raise _ScanAbortedError


def test_scan_pool_outlives_a_failed_job_and_shutdown_cancels_the_queue_and_refuses_new_jobs() -> None:
    pool = _AbandonableScanPool(max_workers=1)
    queued_job = _ScannerJob(scanner=_FixedScanner(name="queued", out=[]), overlay="")
    failed = pool.submit(_abort, queued_job)
    hung = pool.submit(_run_job_closing_connections, _ScannerJob(scanner=_HungScanner(), overlay=""))
    deadline = time.monotonic() + 5
    while not hung.running() and time.monotonic() < deadline:
        time.sleep(0.01)
    queued = pool.submit(_run_job_closing_connections, queued_job)

    pool.shutdown()

    assert isinstance(failed.exception(timeout=0), _ScanAbortedError)
    assert hung.running()
    assert queued.cancelled()
    with pytest.raises(RuntimeError, match="shut down"):
        pool.submit(_run_job_closing_connections, queued_job)


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
    max_seen: list[int] = []

    def _capped_pool(*, max_workers: int) -> _AbandonableScanPool:
        max_seen.append(max_workers)
        return _AbandonableScanPool(max_workers=max_workers)

    jobs = [_ScannerJob(scanner=_FixedScanner(name=f"s{i}", out=[]), overlay="") for i in range(200)]
    cpu = os.cpu_count() or 4
    expected_cap = min(200, cpu * 4)

    with patch("teatree.loop.phases.scan._AbandonableScanPool", side_effect=_capped_pool):
        scan_phase(jobs)

    assert max_seen, "scan pool was not called"
    assert max_seen[0] <= expected_cap


_CALLER_MARK: ContextVar[str] = ContextVar("caller_mark", default="")


@dataclass(slots=True)
class _ContextReadingScanner:
    name: str = "context-reader"

    def scan(self) -> list[ScanSignal]:
        return [ScanSignal(kind="my_pr.open", summary=_CALLER_MARK.get())]


def test_live_scan_does_not_carry_the_callers_context_into_scanners() -> None:
    token = _CALLER_MARK.set("caller")
    try:
        outcome = scan_phase([_ScannerJob(scanner=_ContextReadingScanner(), overlay="")])
    finally:
        _CALLER_MARK.reset(token)

    assert [signal.summary for signal in outcome.signals] == [""]
