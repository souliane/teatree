# test-path: cross-cutting — tests tests/_thread_db_sentinel.py (test infra); the src imports are the
# anti-vacuity subject, not the unit under test.
"""Tests for ``tests/_thread_db_sentinel.py`` — the stranded-DB-handle sentinel.

The sentinel converts a non-deterministic bystander red (a
``PytestUnraisableExceptionWarning`` on whatever test the GC happened to
interrupt) into a named failure on the thread that stranded the handle. These
tests pin BOTH directions at the real seam — a real worker thread, a real
Django connection — so the guard cannot silently become vacuous:

* a thread that does NOT close its handle is detected;
* the same thread WITH ``close_thread_db_connections`` is not;
* neutering that helper at a real production call site (``teatree.loop.phases.scan``) goes red;
* Django's async-unsafe guard is what refuses an event-loop thread a connection, and
``DJANGO_ALLOW_ASYNC_UNSAFE`` turns that refusal into a stranded handle.
"""

import asyncio
import dataclasses
import os
import sqlite3
import threading
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from django.core.exceptions import SynchronousOnlyOperation
from django.db import connection
from django.db.backends.base.base import BaseDatabaseWrapper
from django.test import TestCase

from teatree.loop.phases import scan
from teatree.utils.thread_db import close_thread_db_connections
from tests._thread_db_sentinel import (
    ASYNC_UNSAFE_ENV,
    AsyncUnsafeGuardDisabledError,
    OpenedConnection,
    StrandedDbHandleError,
    ThreadDbHandleSentinel,
    assert_async_unsafe_guard_intact,
    describe,
    handle_is_open,
)

if TYPE_CHECKING:
    from collections.abc import Callable


def _recording_sentinel(monkeypatch: pytest.MonkeyPatch) -> ThreadDbHandleSentinel:
    """A sentinel wired to record opens for the duration of one test.

    Wraps ``connect`` through ``monkeypatch`` rather than the plugin's own
    ``install()`` so the class is restored afterwards and the always-on suite
    sentinel is left untouched.
    """
    sentinel = ThreadDbHandleSentinel()
    original = BaseDatabaseWrapper.connect

    def connect(wrapper: Any) -> Any:
        result = original(wrapper)
        sentinel.record_open(wrapper)
        return result

    monkeypatch.setattr(BaseDatabaseWrapper, "connect", connect)
    return sentinel


def _open_this_threads_connection() -> None:
    """Open the CALLING thread's connection.

    ``django.db.connection`` is a thread-local proxy, so this must be resolved on
    the worker thread — passing ``connection.ensure_connection`` in from the main
    thread would bind (and reuse) the main thread's already-open wrapper.
    """
    connection.ensure_connection()


def _run_on_worker(target: "Callable[[], object]") -> None:
    thread = threading.Thread(target=target, name="sentinel-subject")
    thread.start()
    thread.join()


def _run_scan_job_on_a_worker(sentinel: ThreadDbHandleSentinel, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Drive the real ``scan_phase`` pool-worker wrapper with an ORM-touching job."""

    def _job_that_touches_the_orm(job: object) -> tuple[object, list[object], str]:
        _open_this_threads_connection()
        return (job, [], "")

    monkeypatch.setattr(scan, "_run_job", _job_that_touches_the_orm)
    _run_on_worker(lambda: scan._run_job_closing_connections("job"))
    return [record.thread.name for record in sentinel.sweep()]


@dataclasses.dataclass(slots=True)
class _Item:
    """The minimal ``pytest.Item`` surface the teardown hook reads."""

    nodeid: str


class TestSentinelDetectsAStrandedHandle(TestCase):
    """A worker thread that exits holding an open handle is named, not tolerated."""

    @pytest.fixture(autouse=True)
    def _fixtures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Grouped into a TestCase per souliane/teatree#98; monkeypatch injected."""
        self.monkeypatch = monkeypatch

    def test_a_thread_that_never_closes_is_reported(self) -> None:
        sentinel = _recording_sentinel(self.monkeypatch)

        _run_on_worker(_open_this_threads_connection)

        assert [record.thread.name for record in sentinel.sweep()] == ["sentinel-subject"]

    def test_a_thread_that_closes_is_not_reported(self) -> None:
        sentinel = _recording_sentinel(self.monkeypatch)

        def _clean() -> None:
            try:
                _open_this_threads_connection()
            finally:
                close_thread_db_connections()

        _run_on_worker(_clean)

        assert sentinel.sweep() == []

    def test_a_live_thread_is_left_under_watch_not_flagged(self) -> None:
        """A running worker's connection is in use, not stranded."""
        sentinel = _recording_sentinel(self.monkeypatch)
        release = threading.Event()
        opened = threading.Event()

        def _hold() -> None:
            try:
                _open_this_threads_connection()
                opened.set()
                release.wait(timeout=10)
            finally:
                close_thread_db_connections()

        thread = threading.Thread(target=_hold, name="sentinel-live", daemon=True)
        thread.start()
        assert opened.wait(timeout=10), "the worker never opened its connection"
        try:
            assert sentinel.sweep() == []
        finally:
            release.set()
            thread.join(timeout=10)

    def test_sweep_releases_the_handle_so_no_bystander_inherits_it(self) -> None:
        """The whole point: the leak dies here instead of GC-ing into another test."""
        sentinel = _recording_sentinel(self.monkeypatch)

        _run_on_worker(_open_this_threads_connection)
        stranded = sentinel.sweep()

        assert stranded, "precondition: the sweep must have found the stranded handle"
        assert all(record.wrapper.connection is None for record in stranded)
        assert sentinel.sweep() == [], "a released handle must not be reported twice"


class TestSentinelFailsTheRun(TestCase):
    """The teardown hook turns a detected strand into a real, named failure."""

    @pytest.fixture(autouse=True)
    def _fixtures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Grouped into a TestCase per souliane/teatree#98; monkeypatch injected."""
        self.monkeypatch = monkeypatch

    def test_teardown_raises_and_names_the_opening_test(self) -> None:
        sentinel = _recording_sentinel(self.monkeypatch)

        _run_on_worker(_open_this_threads_connection)

        hook = sentinel.pytest_runtest_teardown(_Item("tests/some_module.py::test_victim"))
        next(hook)
        with pytest.raises(StrandedDbHandleError) as excinfo:
            hook.send(None)

        message = str(excinfo.value)
        assert "close_thread_db_connections" in message
        assert "sentinel-subject" in message
        assert "tests/some_module.py::test_victim" in message

    def test_teardown_is_silent_when_nothing_leaked(self) -> None:
        sentinel = _recording_sentinel(self.monkeypatch)

        hook = sentinel.pytest_runtest_teardown(_Item("tests/some_module.py::test_clean"))
        next(hook)
        with pytest.raises(StopIteration):
            hook.send(None)


class TestNeuteringTheHelperGoesRed(TestCase):
    """Anti-vacuity: a real production call site reds the sentinel once un-wired.

    ``teatree.loop.phases.scan._run_job_closing_connections`` is one of the pool
    workers PR #3536 wired. Removing its ``close_thread_db_connections`` call —
    exactly the regression this sentinel exists to catch — must be detected.
    """

    @pytest.fixture(autouse=True)
    def _fixtures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Grouped into a TestCase per souliane/teatree#98; monkeypatch injected."""
        self.monkeypatch = monkeypatch

    def test_the_wired_site_is_clean(self) -> None:
        sentinel = _recording_sentinel(self.monkeypatch)

        assert _run_scan_job_on_a_worker(sentinel, self.monkeypatch) == []

    def test_the_same_site_with_the_helper_neutered_is_caught(self) -> None:
        sentinel = _recording_sentinel(self.monkeypatch)
        self.monkeypatch.setattr(scan, "close_thread_db_connections", lambda: None)

        assert _run_scan_job_on_a_worker(sentinel, self.monkeypatch) == ["sentinel-subject"]


def _open_a_connection_inside_an_event_loop() -> None:
    """Open this thread's connection from inside a running event loop."""

    async def _open() -> None:
        await asyncio.sleep(0)  # the loop must be running when connect() checks
        connection.ensure_connection()

    asyncio.run(_open())


def _run_on_worker_capturing(target: "Callable[[], object]") -> BaseException | None:
    """Run *target* on a worker thread; return the exception it raised, if any."""
    raised: list[BaseException] = []

    def _wrapped() -> None:
        try:
            target()
        except BaseException as exc:  # noqa: BLE001 — the exception IS the assertion subject
            raised.append(exc)

    thread = threading.Thread(target=_wrapped, name="sentinel-subject")
    thread.start()
    thread.join()
    return raised[0] if raised else None


class TestAsyncUnsafeGuardAssertion:
    """The unit lane refuses to run with Django's async-unsafe guard switched off."""

    def test_silent_when_the_variable_is_absent(self) -> None:
        assert assert_async_unsafe_guard_intact({}) is None

    def test_silent_when_the_variable_is_empty(self) -> None:
        assert assert_async_unsafe_guard_intact({ASYNC_UNSAFE_ENV: ""}) is None

    def test_raises_and_explains_the_consequence_when_set(self) -> None:
        with pytest.raises(AsyncUnsafeGuardDisabledError) as excinfo:
            assert_async_unsafe_guard_intact({ASYNC_UNSAFE_ENV: "1"})

        message = str(excinfo.value)
        assert "no such table" in message
        assert "conftest" in message, "the message must name the collection-time import that sets it"

    def test_the_live_environment_is_clean(self) -> None:
        """This suite is itself the subject: a stray import would set it for everyone."""
        assert not os.environ.get(ASYNC_UNSAFE_ENV)


class TestAsyncUnsafeGuardIsWhatStopsTheStrand(TestCase):
    """Anti-vacuity for the guard: flipping the variable produces the real leak.

    Django decorates ``BaseDatabaseWrapper.connect`` with ``@async_unsafe``, so a
    thread that owns a running event loop is refused a connection. That refusal —
    not any ``finally`` — is what keeps such a thread from stranding a handle.
    """

    @pytest.fixture(autouse=True)
    def _fixtures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Grouped into a TestCase per souliane/teatree#98; monkeypatch injected."""
        self.monkeypatch = monkeypatch

    def test_an_event_loop_thread_is_refused_a_connection(self) -> None:
        monkeypatch = self.monkeypatch
        sentinel = _recording_sentinel(monkeypatch)
        monkeypatch.delenv(ASYNC_UNSAFE_ENV, raising=False)

        raised = _run_on_worker_capturing(_open_a_connection_inside_an_event_loop)

        assert isinstance(raised, SynchronousOnlyOperation)
        assert sentinel.sweep() == [], "a refused connect strands nothing"

    def test_disabling_the_guard_strands_the_handle(self) -> None:
        monkeypatch = self.monkeypatch
        sentinel = _recording_sentinel(monkeypatch)
        monkeypatch.setenv(ASYNC_UNSAFE_ENV, "1")

        raised = _run_on_worker_capturing(_open_a_connection_inside_an_event_loop)

        assert raised is None, "with the guard off the connect succeeds instead of raising"
        assert [record.thread.name for record in sentinel.sweep()] == ["sentinel-subject"]


class TestHandleIsOpen:
    """The open/closed probe answers without executing SQL on an ownerless handle."""

    def test_tracks_a_real_sqlite_handle_through_close(self) -> None:
        raw = sqlite3.connect(":memory:")

        assert handle_is_open(raw)

        raw.close()

        assert not handle_is_open(raw)

    def test_reads_the_closed_flag_when_the_backend_exposes_one(self) -> None:
        assert handle_is_open(SimpleNamespace(closed=False))
        assert not handle_is_open(SimpleNamespace(closed=True))


class TestDescribe:
    """The failure message has to be actionable without re-running anything."""

    def test_names_the_fix_and_both_endpoints(self) -> None:
        record = OpenedConnection(
            wrapper=None,
            thread=threading.current_thread(),
            nodeid="tests/a.py::test_a",
            stack="  frame\n",
        )

        message = describe([record], detected_in="tests/b.py::test_b")

        assert "close_thread_db_connections" in message
        assert "tests/a.py::test_a" in message
        assert "tests/b.py::test_b" in message
