"""Tests for ``teatree.utils.thread_db`` — per-thread Django DB connection hygiene."""

import sqlite3
import threading

import pytest
from django.db import connection, connections
from django.test import TestCase

from teatree.utils.thread_db import close_thread_db_connections


def _open_connection_and_close_thread_connections() -> sqlite3.Connection:
    """Open this thread's Django connection, then run the helper; return the raw handle."""
    connection.ensure_connection()
    raw = connection.connection
    close_thread_db_connections()
    return raw


def _run_on_worker_thread(target: "object") -> object:
    """Run *target* on a throwaway worker thread and return its result."""
    captured: list[object] = []
    thread = threading.Thread(target=lambda: captured.append(target()))  # ty: ignore[call-non-callable]
    thread.start()
    thread.join()
    return captured[0]


def _is_closed(raw: sqlite3.Connection) -> bool:
    """A closed sqlite3 connection raises ``ProgrammingError`` when used."""
    try:
        raw.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return True
    return False


class TestCloseThreadDbConnections(TestCase):
    """The helper releases a worker thread's raw handle, and never the main thread's."""

    def test_closes_a_worker_threads_raw_db_handle(self) -> None:
        raw = _run_on_worker_thread(_open_connection_and_close_thread_connections)

        assert isinstance(raw, sqlite3.Connection)
        assert _is_closed(raw), "the worker thread's raw DB handle was left open"

    def test_dereferences_the_wrapper_so_django_reopens(self) -> None:
        def _wrapper_after_close() -> object:
            connection.ensure_connection()
            close_thread_db_connections()
            return connection.connection

        assert _run_on_worker_thread(_wrapper_after_close) is None

    def test_refuses_to_close_the_main_threads_connection(self) -> None:
        """The main thread owns the TestCase's transaction — closing it would break the suite."""
        connection.ensure_connection()
        raw = connection.connection

        close_thread_db_connections()

        assert not _is_closed(raw), "the main thread's connection must survive"
        assert connection.connection is raw

    def test_is_a_no_op_for_a_thread_that_never_opened_a_connection(self) -> None:
        def _never_touches_the_orm() -> object:
            close_thread_db_connections()
            return [c.connection for c in connections.all(initialized_only=True)]

        assert _run_on_worker_thread(_never_touches_the_orm) in ([], [None])


class TestCloseThreadDbConnectionsIsNotVacuous(TestCase):
    """Django's own ``close_all()`` does NOT release the handle — hence this helper."""

    def test_django_close_all_leaves_the_in_memory_handle_open(self) -> None:
        """Pins the Django behaviour the helper exists to work around.

        ``DatabaseWrapper.close()`` is a documented no-op for an in-memory sqlite
        database. If a future Django ever changes that, this test goes red and the
        helper's purpose should be re-examined.
        """
        if not connection.is_in_memory_db():
            pytest.skip("only meaningful against the in-memory test database")

        def _close_all_the_django_way() -> object:
            connection.ensure_connection()
            raw = connection.connection
            connections.close_all()
            return raw

        raw = _run_on_worker_thread(_close_all_the_django_way)
        assert isinstance(raw, sqlite3.Connection)
        try:
            assert not _is_closed(raw), "close_all() unexpectedly released the handle"
        finally:
            # This test deliberately reproduces the leak, so it must clean up after
            # itself — an unclosed handle here would be finalized at a later GC and
            # fail an unrelated test, which is the very bug under test.
            raw.close()
