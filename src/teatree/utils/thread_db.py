"""Per-thread Django DB connection hygiene for teatree's worker threads.

Every thread that touches the ORM gets its OWN thread-local Django connection
(``django.db.connections`` is backed by a thread-critical ``asgiref.local.Local``).
A worker thread that opens one and exits without closing it strands the raw
``sqlite3.Connection``, which CPython then finalizes at an arbitrary later GC —
emitting ``ResourceWarning: unclosed database``. Under ``filterwarnings = error``
that warning is a hard failure attributed to whatever unrelated test happened to
be running in that xdist worker when the GC fired, which is why the symptom
presents as a non-deterministic red on a rotating cast of innocent tests.

``connections.close_all()`` does NOT fix this under the test database. Django's
sqlite backend deliberately makes ``close()`` a no-op for an in-memory database,
because closing the last handle would discard the database itself
(``django/db/backends/sqlite3/base.py``: "If database is in memory, closing the
connection destroys the database ... ignore close requests on an in-memory db").
So the raw DB-API handle must be closed directly, which is what this module does.
"""

import threading


def close_thread_db_connections() -> None:
    """Close the calling thread's Django DB connections, raw handle included.

    Call this in a ``finally`` on any worker thread (or pool job) that may have
    touched the ORM, so the thread never strands a DB handle for the garbage
    collector to finalize later. It is a no-op for a thread that never opened a
    connection, and safe to call repeatedly — Django transparently reopens on the
    next query because the wrapper is left dereferenced rather than half-closed.

    The raw handle is closed instead of calling ``connection.close()`` because the
    latter is a documented no-op under the in-memory test database (see the module
    docstring). Closing the raw handle releases it on both the in-memory test
    database and a production file/Postgres database.

    Refuses to run on the main thread. The main thread owns the connection a
    Django ``TestCase`` wraps its transaction in, and under the shared-cache
    in-memory test database closing that handle would tear the test database out
    from under the suite. Only a discardable worker thread's connections are
    this function's business.
    """
    if threading.current_thread() is threading.main_thread():
        return

    from django.db import connections  # noqa: PLC0415 — deferred: Django import at call time

    for conn in connections.all(initialized_only=True):
        raw = conn.connection
        if raw is None:
            continue
        try:
            raw.close()
        finally:
            # Dereference even if close() raised, so Django reopens cleanly rather
            # than reusing a handle in an unknown state.
            conn.connection = None
